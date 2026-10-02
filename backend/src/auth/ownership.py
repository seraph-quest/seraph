"""Private data continuity. Historical owner ids never become live authority.

Identity, proof hashes and selected read scopes live in canonical SQLite. Every
mutation is atomic with its journal; neither a label nor a guessed record id
establishes identity. This module never admits or resumes execution.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import hashlib
import json
import secrets

from sqlalchemy import select, update, text, and_, or_
from pydantic import BaseModel, ConfigDict, Field

import src.db.engine as database
from src.db.models import (
    Goal, GuardianRoutine, Memory, MemoryStatus, OperatorIdentity,
    OperatorSession, OperatorContinuityCredential, OperatorRecoveryJournal,
    WorkBoardTask, WorkBoardInputArtifact, WorkBoardAttempt, WorkBoardStatus, WorkflowRunState,
)
from src.auth.service import AuthFailure, _aware, _token_hash, _ownership_metadata

CONTINUITY_MAX_AGE = 365 * 24 * 60 * 60
MAX_SELECTIONS = 50
MAX_JOURNALS = 100
MAX_RECOVERED_RECORDS = 250
RECOVERED_FIELDS = {
    "ownership_access": "recovered_read_only",
    "execution_block_reason": "current_scope_review_required",
}


def read_scope_clause(id_column, owner_column, current_root, recovered, *, principal_column=None, current_principal=None):
    current = owner_column == current_root
    if principal_column is not None:
        current = and_(current, principal_column == current_principal)
    historical = []
    for identifier, historical_root in recovered.items():
        clause = and_(id_column == identifier, owner_column == historical_root)
        if principal_column is not None:
            # The caller supplies only server-resolved exact selections. The
            # historical pair must still match immutable enrolled-root metadata.
            allowed = select(OperatorSession.id).where(
                OperatorSession.id == owner_column,
                or_(OperatorSession.principal_id == principal_column,
                    OperatorSession.legacy_owner_principal_id == principal_column),
            ).exists()
            clause = and_(clause, allowed)
        historical.append(clause)
    return or_(current, *historical)


def _proved_pair(root, principal):
    return principal == root.principal_id or (
        root.legacy_owner_principal_id is not None and principal == root.legacy_owner_principal_id
    )


async def selected_read_principal(operator, kind, record_id, *, db=None):
    """Original principal for an exact selected object; never live authority."""
    if db is None:
        async with database.get_session() as session:
            return await selected_read_principal(operator, kind, record_id, db=session)
    scopes = await selected_read_scopes(operator, kind, db=db)
    if record_id not in scopes:
        return None
    row = await _resolve(db, operator.operator_identity_id, RecoverySelection(kind=kind, record_id=record_id))
    return getattr(row, "owner_principal_id", None)


class RecoverySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(goal|task|artifact|output_artifact|memory|routine)$")
    record_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selections: list[RecoverySelection] = Field(min_length=1, max_length=MAX_SELECTIONS)


class RecoveryConfirmRequest(RecoveryRequest):
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledge_read_only: bool


_MODELS = {
    "goal": (Goal, "id", "owner_session_id"),
    "task": (WorkBoardTask, "task_id", "owner_session_id"),
    "artifact": (WorkBoardInputArtifact, "artifact_id", "owner_session_id"),
    "memory": (Memory, "id", "source_session_id"),
    "routine": (GuardianRoutine, "id", "owner_session_id"),
    "output_artifact": (None, "record_id", "owner_session_id"),
}


def _now():
    return datetime.now(timezone.utc)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def _write(db):
    # Serialize proof rotation/claim and rollback on the supported SQLite core.
    if db.bind.dialect.name == "sqlite":
        await db.execute(text("BEGIN IMMEDIATE"))


async def _current_root(db, operator):
    root = await db.get(OperatorSession, operator.session_id, populate_existing=True)
    if (
        root is None or root.principal_id != operator.principal.principal_id or root.is_bearer_tombstone or root.revoked_at is not None
        or _now() >= _aware(root.idle_expires_at)
        or _now() >= _aware(root.absolute_expires_at)
        or not operator._token_hash or root.token_hash != operator._token_hash
    ):
        raise AuthFailure("session_revoked")
    return root


async def _identity(db, operator):
    root = await _current_root(db, operator)
    identity = await db.get(OperatorIdentity, root.operator_identity_id) if root.operator_identity_id else None
    if identity is None or identity.revoked_at is not None:
        raise AuthFailure("ownership_proof_required")
    return identity


def _credential(identity_id, kind):
    raw = secrets.token_urlsafe(32)
    row = OperatorContinuityCredential(
        identity_id=identity_id, kind=kind, token_hash=_token_hash(raw),
        expires_at=_now() + timedelta(seconds=CONTINUITY_MAX_AGE),
    )
    return raw, row


async def bind_login_identity(db, *, continuity_token=None, recovery_code=None):
    """Called inside session creation after password verification, never alone."""
    if continuity_token and recovery_code:
        raise AuthFailure("ownership_proof_invalid")
    raw = recovery_code or continuity_token
    kind = "recovery" if recovery_code else "cookie"
    proof = (await db.execute(select(OperatorContinuityCredential).where(
        OperatorContinuityCredential.token_hash == _token_hash(raw),
        OperatorContinuityCredential.kind == kind,
    ))).scalar_one_or_none()
    if proof is None:
        raise AuthFailure("ownership_proof_invalid")
    identity = await db.get(OperatorIdentity, proof.identity_id)
    now = _now()
    if identity is None or identity.revoked_at is not None:
        raise AuthFailure("ownership_proof_invalid")
    claimed = await db.execute(update(OperatorContinuityCredential).where(
        OperatorContinuityCredential.id == proof.id,
        OperatorContinuityCredential.revoked_at.is_(None),
        OperatorContinuityCredential.expires_at > now,
    ).values(revoked_at=now).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise AuthFailure("ownership_proof_invalid")
    token, replacement = _credential(identity.id, "cookie")
    db.add(replacement)
    return identity.id, token


async def enroll(operator):
    async with database.get_session() as db:
        await _write(db)
        root = await _current_root(db, operator)
        if root.operator_identity_id:
            raise AuthFailure("ownership_already_enrolled")
        continuity, _ = await _ownership_metadata(db, root)
        if continuity != "stable":
            raise AuthFailure("legacy_ownership_unproved")
        identity = OperatorIdentity()
        db.add(identity)
        await db.flush()
        root.operator_identity_id = identity.id
        cookie, credential = _credential(identity.id, "cookie")
        code, recovery = _credential(identity.id, "recovery")
        db.add(credential)
        db.add(recovery)
        return identity.id, cookie, code


async def replace_recovery_code(operator):
    async with database.get_session() as db:
        await _write(db)
        identity = await _identity(db, operator)
        await db.execute(update(OperatorContinuityCredential).where(
            OperatorContinuityCredential.identity_id == identity.id,
            OperatorContinuityCredential.kind == "recovery",
            OperatorContinuityCredential.revoked_at.is_(None),
        ).values(revoked_at=_now()))
        code, credential = _credential(identity.id, "recovery")
        db.add(credential)
        return code


async def forget_device(operator, raw_cookie):
    async with database.get_session() as db:
        await _write(db)
        identity = await _identity(db, operator)
        if raw_cookie:
            await db.execute(update(OperatorContinuityCredential).where(
                OperatorContinuityCredential.identity_id == identity.id,
                OperatorContinuityCredential.token_hash == _token_hash(raw_cookie),
                OperatorContinuityCredential.kind == "cookie",
            ).values(revoked_at=_now()))


async def revoke_identity(operator):
    async with database.get_session() as db:
        await _write(db)
        identity = await _identity(db, operator)
        now = _now()
        identity.revoked_at = now
        await db.execute(update(OperatorContinuityCredential).where(
            OperatorContinuityCredential.identity_id == identity.id,
        ).values(revoked_at=now))
        await db.execute(update(OperatorSession).where(
            OperatorSession.operator_identity_id == identity.id,
            OperatorSession.revoked_at.is_(None),
        ).values(revoked_at=now))


def _record_statement(kind, identity_id):
    model, id_field, owner_field = _MODELS[kind]
    statement = select(model).join(OperatorSession, getattr(model, owner_field) == OperatorSession.id).where(
        OperatorSession.operator_identity_id == identity_id,
        OperatorSession.is_bearer_tombstone.is_(False),
    )
    if kind != "memory":
        statement = statement.where(or_(
            model.owner_principal_id == OperatorSession.principal_id,
            and_(OperatorSession.legacy_owner_principal_id.is_not(None), model.owner_principal_id == OperatorSession.legacy_owner_principal_id),
        ))
    if kind in {"task", "artifact"}:
        statement = statement.where(select(Goal.id).where(
            Goal.id == model.goal_id,
            Goal.owner_principal_id == model.owner_principal_id,
            Goal.owner_session_id == model.owner_session_id,
        ).exists())
    if kind == "memory":
        from src.memory.repository import _canonical_memory_without_tombstone_clause, _canonical_memory_without_deletion_marker_clause
        statement = statement.where(
            model.status == MemoryStatus.active, model.last_confirmed_at.is_not(None),
            _canonical_memory_without_tombstone_clause(), _canonical_memory_without_deletion_marker_clause(),
        )
    if kind == "artifact":
        statement = statement.where(model.state.notin_(["deleted", "revoked", "redacted"]))
    return statement


async def _resolve(db, identity_id, selection):
    if selection.kind == "output_artifact":
        try:
            from src.memory.evidence_sources import output_artifact_scope
        except ModuleNotFoundError as exc:
            raise AuthFailure("output_artifact_evidence_unavailable") from exc
        evidence = await output_artifact_scope(db, selection.record_id)
        root = await db.get(OperatorSession, evidence["owner_session_id"]) if evidence else None
        if (
            not evidence or not root or root.is_bearer_tombstone
            or root.operator_identity_id != identity_id
            or not _proved_pair(root, evidence.get("owner_principal_id"))
        ):
            raise AuthFailure("recovery_record_unavailable")
        return SimpleNamespace(**evidence, label="Verified output artifact", revision=evidence["version"])
    model, field, _ = _MODELS[selection.kind]
    row = (await db.execute(_record_statement(selection.kind, identity_id).where(
        getattr(model, field) == selection.record_id,
    ))).scalar_one_or_none()
    if row is None:
        raise AuthFailure("recovery_record_unavailable")
    return row


async def _safe_preview(db, kind, row):
    from src.vault.redaction import redact_secrets_in_text_readonly
    _, field, owner_field = _MODELS[kind]
    label = str(getattr(row, "title", None) or getattr(row, "name", None) or getattr(row, "summary", None) or getattr(row, "label", None) or "Input artifact")[:240]
    label = await redact_secrets_in_text_readonly(db, label, fail_closed=True)
    return {
        "kind": kind, "record_id": getattr(row, field),
        "source_session_id": getattr(row, owner_field),
        "source_principal_id": getattr(row, "owner_principal_id", None), "label": label,
        "revision": int(getattr(row, "task_revision", None) or getattr(row, "revision", 1)),
        "state": str(getattr(getattr(row, "status", None), "value", getattr(row, "status", None)) or getattr(row, "state", "stored")),
        "record_digest": getattr(row, "digest", None) or _digest(row.model_dump(mode="json")),
        **RECOVERED_FIELDS,
    }


async def inventory(operator):
    async with database.get_session() as db:
        identity = await _identity(db, operator)
        items = []
        truncated = False
        for kind, (model, field, owner_field) in _MODELS.items():
            if kind == "output_artifact":
                continue
            rows = (await db.execute(_record_statement(kind, identity.id).where(
                or_(getattr(model, owner_field) != operator.session_id,
                    model.owner_principal_id != operator.principal.principal_id) if kind != "memory" else getattr(model, owner_field) != operator.session_id,
            ).order_by(getattr(model, field)).limit(MAX_SELECTIONS + 1))).scalars().all()
            truncated |= len(rows) > MAX_SELECTIONS
            for row in rows[:MAX_SELECTIONS]:
                items.append(await _safe_preview(db, kind, row))
        journals = (await db.execute(select(OperatorRecoveryJournal).where(
            OperatorRecoveryJournal.identity_id == identity.id,
        ).order_by(OperatorRecoveryJournal.created_at.desc()).limit(MAX_JOURNALS))).scalars().all()
        return {"records": items, "journals": [journal_payload(row) for row in journals],
                "truncated": truncated, "legacy_recovery": "blocked_missing_private_ownership_proof"}


async def _preview(db, operator, request):
    identity = await _identity(db, operator)
    keys = [(item.kind, item.record_id) for item in request.selections]
    if len(set(keys)) != len(keys):
        raise AuthFailure("recovery_duplicate_selection")
    records = []
    for item in sorted(request.selections, key=lambda entry: (entry.kind, entry.record_id)):
        row = await _resolve(db, identity.id, item)
        _, _, owner_field = _MODELS[item.kind]
        if getattr(row, owner_field) == operator.session_id and (item.kind == "memory" or getattr(row, "owner_principal_id", None) == operator.principal.principal_id):
            raise AuthFailure("recovery_current_scope")
        records.append(await _safe_preview(db, item.kind, row))
    digest = _digest({"identity_id": identity.id, "records": records})
    return identity, records, digest


async def preview(operator, request):
    async with database.get_session() as db:
        _, records, digest = await _preview(db, operator, request)
        return {"records": records, "preview_digest": digest, "restores_execution_authority": False}


def journal_payload(journal):
    return {"journal_id": journal.id, "state": journal.state, "selections": json.loads(journal.selections_json),
            "fresh_work": json.loads(journal.fresh_work_json) if journal.fresh_work_json else None,
            "restores_execution_authority": False}


async def confirm(operator, request):
    if not request.acknowledge_read_only:
        raise AuthFailure("recovery_acknowledgement_required")
    async with database.get_session() as db:
        await _write(db)
        identity = await _identity(db, operator)
        requested_digest = _digest(request.model_dump(mode="json"))
        existing = (await db.execute(select(OperatorRecoveryJournal).where(
            OperatorRecoveryJournal.identity_id == identity.id,
            OperatorRecoveryJournal.idempotency_key == request.idempotency_key,
        ))).scalar_one_or_none()
        if existing:
            if existing.request_digest != requested_digest:
                raise AuthFailure("recovery_idempotency_conflict")
            return journal_payload(existing)
        count = len((await db.execute(select(OperatorRecoveryJournal.id).where(
            OperatorRecoveryJournal.identity_id == identity.id,
        ).limit(MAX_JOURNALS))).scalars().all())
        if count >= MAX_JOURNALS:
            raise AuthFailure("recovery_journal_limit")
        previous = (await db.execute(select(OperatorRecoveryJournal.selections_json).where(
            OperatorRecoveryJournal.identity_id == identity.id,
            OperatorRecoveryJournal.state == "confirmed",
        ))).scalars().all()
        selected_keys = {(entry["kind"], entry["record_id"]) for raw in previous for entry in json.loads(raw)}
        selected_keys.update((entry.kind, entry.record_id) for entry in request.selections)
        if len(selected_keys) > MAX_RECOVERED_RECORDS:
            raise AuthFailure("recovery_selection_limit")
        _, records, digest = await _preview(db, operator, request)
        if digest != request.preview_digest:
            raise AuthFailure("recovery_preview_stale")
        journal = OperatorRecoveryJournal(
            identity_id=identity.id, current_session_id=operator.session_id,
            idempotency_key=request.idempotency_key, request_digest=requested_digest,
            selections_json=json.dumps([{k: row[k] for k in ("kind", "record_id", "source_session_id", "source_principal_id")} for row in records]),
        )
        db.add(journal)
        await db.flush()
        return journal_payload(journal)


async def _journal(db, operator, journal_id):
    identity = await _identity(db, operator)
    row = (await db.execute(select(OperatorRecoveryJournal).where(
        OperatorRecoveryJournal.id == journal_id,
        OperatorRecoveryJournal.identity_id == identity.id,
    ))).scalar_one_or_none()
    if row is None:
        raise AuthFailure("recovery_record_unavailable")
    return row


async def rollback(operator, journal_id):
    async with database.get_session() as db:
        await _write(db)
        journal = await _journal(db, operator, journal_id)
        if journal.fresh_work_json:
            # Fresh records belong to the current scope; rollback cannot hide
            # new intent after its operator has acted on it.
            raise AuthFailure("fresh_work_requires_current_scope_review")
        journal.state = "rolled_back"
        journal.rolled_back_at = journal.rolled_back_at or _now()
        return journal_payload(journal)


async def selected_read_scopes(operator, kind, *, db=None):
    """Server-only exact id -> historical owner mapping, only for canonical GET.

    Revalidate identity and each immutable root binding before returning ids.
    Never call from authenticate_session, mutations, scheduler or dispatch.
    """
    if not operator.operator_identity_id:
        return {}
    if db is None:
        async with database.get_session() as session:
            return await selected_read_scopes(operator, kind, db=session)
    identity = await _identity(db, operator)
    journals = (await db.execute(select(OperatorRecoveryJournal).where(
        OperatorRecoveryJournal.identity_id == identity.id,
        OperatorRecoveryJournal.state == "confirmed",
    ).order_by(OperatorRecoveryJournal.created_at).limit(MAX_JOURNALS))).scalars().all()
    scopes = {}
    roots = {}
    for journal in journals:
        for item in json.loads(journal.selections_json):
            if item["kind"] != kind:
                continue
            if item["record_id"] in scopes:
                continue
            if item["source_session_id"] not in roots:
                roots[item["source_session_id"]] = await db.get(OperatorSession, item["source_session_id"])
            root = roots[item["source_session_id"]]
            if root and not root.is_bearer_tombstone and root.operator_identity_id == identity.id:
                try:
                    row = await _resolve(db, identity.id, RecoverySelection(kind=kind, record_id=item["record_id"]))
                    if getattr(row, _MODELS[kind][2]) != item["source_session_id"] or getattr(row, "owner_principal_id", None) != item.get("source_principal_id"):
                        continue
                    if root.id == operator.session_id and (kind == "memory" or item.get("source_principal_id") == operator.principal.principal_id):
                        continue
                except AuthFailure:
                    continue
                scopes[item["record_id"]] = root.id
    return scopes


async def _verified_completed_snapshot_citation(db, task, selections):
    """A verified local snapshot can seed empty intent; no attempt is replayed."""
    from src.work_board.dispatcher import GOAL_SNAPSHOT_CAPABILITY
    if task.status != WorkBoardStatus.done or task.capability_id != GOAL_SNAPSHOT_CAPABILITY or task.block_kind:
        return False
    try:
        from src.memory.evidence_sources import verified_task_outputs, output_artifact_scope
    except ModuleNotFoundError:
        raise AuthFailure("output_artifact_evidence_unavailable")
    attempts = (await db.execute(select(WorkBoardAttempt).where(
        WorkBoardAttempt.task_id == task.task_id,
    ).order_by(WorkBoardAttempt.created_at.desc()).limit(2))).scalars().all()
    # Multiple-attempt reconciliation remains outside this narrow local proof.
    if len(attempts) != 1 or attempts[0].outcome != "verified" or attempts[0].ended_at is None:
        return False
    attempt = attempts[0]
    parent = (await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == attempt.workflow_run_id,
    ))).scalar_one_or_none()
    if parent is None:
        return False
    outputs = await verified_task_outputs(db, task, attempt, parent)
    for _, receipt in outputs:
        artifact_id = receipt.get("artifact_id")
        if not any(item["kind"] == "output_artifact" and item["record_id"] == artifact_id for item in selections):
            continue
        proof = await output_artifact_scope(db, artifact_id)
        if proof and proof["task_id"] == task.task_id and proof["goal_id"] == task.goal_id and proof["owner_principal_id"] == task.owner_principal_id and proof["owner_session_id"] == task.owner_session_id:
            return True
    return False


async def fresh_work(operator, journal_id):
    """New triage intent, never a retry of historical effects or paid work."""
    from src.work_board.repository import WorkBoardRepository
    from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate
    repository = WorkBoardRepository()
    async with database.get_session() as db:
        await _write(db)
        journal = await _journal(db, operator, journal_id)
        if journal.state != "confirmed":
            raise AuthFailure("recovery_rolled_back")
        if journal.fresh_work_json:
            # Returning old fresh intent metadata is discovery; it is never
            # rebound to this login or readmitted for execution.
            return journal_payload(journal)
        selections = json.loads(journal.selections_json)
        original_tasks = []
        original_goals = []
        for item in selections:
            row = await _resolve(db, journal.identity_id, RecoverySelection.model_validate({k: item[k] for k in ("kind", "record_id")}))
            if item["kind"] == "task":
                # Past effects require reconciliation. The sole bounded
                # exception is verified local snapshot citation into empty
                # intent; its exact output must be explicitly selected.
                attempted = await db.scalar(select(WorkBoardAttempt.attempt_id).where(WorkBoardAttempt.task_id == row.task_id).limit(1))
                if row.block_kind in {"unknown_effect", "cost_liability", "reconcile_admission_binding"} or row.status in {WorkBoardStatus.running, WorkBoardStatus.review}:
                    raise AuthFailure("historical_effect_reconciliation_required")
                if attempted and not await _verified_completed_snapshot_citation(db, row, selections):
                    raise AuthFailure("historical_effect_reconciliation_required")
                original_tasks.append(row)
            elif item["kind"] == "goal":
                original_goals.append(row)
        if len(original_tasks) + len(original_goals) > 10:
            raise AuthFailure("fresh_work_selection_limit")
        if not original_tasks and not original_goals:
            raise AuthFailure("fresh_work_goal_or_task_required")
        goal_map = {}
        created = {"current_session_id": operator.session_id, "goals": [], "tasks": [], "historical_citations": selections}
        async def copy_goal(old):
            if old.id in goal_map:
                return goal_map[old.id]
            goal = Goal(title=old.title, description=old.description, level=old.level, domain=old.domain,
                        owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id,
                        proactive_enabled=False, admission_budget_json=None, success_criterion_json=None)
            db.add(goal)
            await db.flush()
            goal.path = f"/{goal.id}/"
            goal_map[old.id] = goal
            created["goals"].append({"id": goal.id, "source_goal_id": old.id})
            return goal
        for goal in original_goals:
            await copy_goal(goal)
        for task in original_tasks:
            old_goal = await _resolve(db, journal.identity_id, RecoverySelection(kind="goal", record_id=task.goal_id))
            goal = await copy_goal(old_goal)
            citations = f"Historical task {task.task_id}; goal {old_goal.id}."
            if task.input_artifact_id and any(item["kind"] == "artifact" and item["record_id"] == task.input_artifact_id for item in selections):
                # Citation remains protected by exact artifact GET selection.
                citations += f" Input artifact /api/work-board/input-artifacts/{task.input_artifact_id} (historical read only)."
            mutation = await repository.create_task(db, WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id),
                WorkBoardTaskCreate(title=task.title, body=(task.body[:3400] + "\n" + citations)[:4000],
                                    goal_id=goal.id, goal_revision=1, status=WorkBoardStatus.triage,
                                    idempotency_key=f"recovery:{journal.id}:{task.task_id}", requires_review=True),
                origin_session_id=operator.session_id)
            created["tasks"].append({"task_id": mutation.task.task_id, "source_task_id": task.task_id})
        journal.fresh_work_json = json.dumps(created)
        return journal_payload(journal)


async def fresh_work_source_scope(operator, current_task_id, kind, record_id, *, db=None):
    """Exact locally reviewed source for a current fresh intent, never egress.

    The caller must separately enforce source-purpose model consent. A selected
    historical read alone does not authorize recall for arbitrary current jobs.
    """
    if db is None:
        async with database.get_session() as session:
            return await fresh_work_source_scope(operator, current_task_id, kind, record_id, db=session)
    if kind not in _MODELS or not operator.operator_identity_id:
        return None
    identity = await _identity(db, operator)
    current = (await db.execute(select(WorkBoardTask).where(
        WorkBoardTask.task_id == current_task_id,
        WorkBoardTask.owner_principal_id == operator.principal.principal_id,
        WorkBoardTask.owner_session_id == operator.session_id,
    ))).scalar_one_or_none()
    if current is None:
        return None
    journals = (await db.execute(select(OperatorRecoveryJournal).where(
        OperatorRecoveryJournal.identity_id == identity.id,
        OperatorRecoveryJournal.state == "confirmed",
        OperatorRecoveryJournal.fresh_work_json.is_not(None),
    ).limit(MAX_JOURNALS))).scalars().all()
    for journal in journals:
        fresh = json.loads(journal.fresh_work_json)
        if fresh.get("current_session_id") != operator.session_id:
            continue
        task_line = next((item for item in fresh.get("tasks", []) if item["task_id"] == current_task_id), None)
        goal_line = next((item for item in fresh.get("goals", []) if item["id"] == current.goal_id), None)
        if not task_line or not goal_line:
            continue
        selected = next((item for item in json.loads(journal.selections_json) if item["kind"] == kind and item["record_id"] == record_id), None)
        if not selected:
            continue
        scopes = await selected_read_scopes(operator, kind, db=db)
        if record_id not in scopes:
            return None
        row = await _resolve(db, identity.id, RecoverySelection(kind=kind, record_id=record_id))
        try:
            source_task = await _resolve(db, identity.id, RecoverySelection(kind="task", record_id=task_line["source_task_id"]))
        except AuthFailure:
            return None
        if source_task.goal_id != goal_line["source_goal_id"]:
            return None
        if kind == "goal":
            matches = record_id == goal_line["source_goal_id"]
        elif kind == "task":
            matches = record_id == task_line["source_task_id"] and row.goal_id == goal_line["source_goal_id"]
        elif kind in {"artifact", "output_artifact"}:
            matches = row.goal_id == goal_line["source_goal_id"]
            if kind == "output_artifact":
                matches = matches and row.task_id == task_line["source_task_id"]
        elif kind == "memory":
            # Memory lineage is evidence-source-specific; no root-wide adoption.
            from src.db.models import MemoryProposal
            matches = bool(await db.scalar(select(MemoryProposal.proposal_id).where(
                MemoryProposal.accepted_memory_id == record_id,
                MemoryProposal.owner_session_id == scopes[record_id],
                MemoryProposal.owner_principal_id == source_task.owner_principal_id,
                MemoryProposal.goal_id == goal_line["source_goal_id"],
                MemoryProposal.source_task_id == task_line["source_task_id"],
                MemoryProposal.status == "accepted",
            ).limit(1)))
        else:
            matches = getattr(row, "goal_id", None) == goal_line["source_goal_id"]
        return scopes[record_id] if matches else None
    return None
