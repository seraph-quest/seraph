"""Daily, passive programme Inbox receipts on the existing Python owners.

There is no inference, source fetch, executor or independent queue here. Finding
content is reopened through Discovery's physical/current-authority readback.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text, func

from config.settings import settings
from src.auth.service import authenticate_principal
from src.db import engine as database
from src.db.models import (Goal, OperatorIdentity, OperatorSession, ProgrammeDigestReceipt,
    ProgrammeFollowThrough, ProgrammeFindingAction, ProgrammeNotificationPreference, WorkflowRunState,
    InferenceCostReservation, NativeNotificationOutbox)
from src.guardian.goal_programmes import GoalProgrammeError, _load, _aware, goal_programme_service
from src.guardian.research_plan_contracts import ArtifactRef


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProgrammeDigestV1(Closed):
    local_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    timezone: str
    programme_ids: list[str] = Field(max_length=128)
    finding_ids: list[str] = Field(max_length=1024)  # 128 programmes × eight closed Brief findings.
    prepared_outputs: list[ArtifactRef] = Field(max_length=512)  # Four original prepared refs per programme.
    blocked_reasons: list[str] = Field(max_length=128)

    @field_validator("timezone")
    @classmethod
    def iana_timezone(cls, value):
        ZoneInfo(value)
        return value


class DeadlineNegativeMemo(Closed):
    key: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: Literal["reviewed_category_no_match", "deadline_source_requires_current_readback"]


class FollowThroughIntent(Closed):
    finding_id: str
    desired_outcome: str = Field(max_length=2000)
    task_proposal_id: str | None
    due_at: datetime | None
    status: Literal["pending", "prepared", "deferred", "dismissed", "completed", "blocked"]


class FindingAction(Closed):
    action: Literal["accept_followup", "snooze", "dismiss"]
    desired_outcome: str | None = Field(default=None, min_length=1, max_length=2000)
    until: datetime | None = None
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")

    @field_validator("until")
    @classmethod
    def aware_date(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("follow-up date must include timezone")
        return value


class NotificationPreference(Closed):
    enabled: bool = Field(strict=True)
    deadline_categories: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("deadline_categories")
    @classmethod
    def safe_categories(cls, values):
        if len(set(values)) != len(values) or any(not re.fullmatch(r"[a-z][a-z0-9 -]{0,63}", value) for value in values):
            raise ValueError("exact bounded lower-case opportunity categories required")
        return values


def local_clock(now: datetime):
    # Invalid operator configuration is blocked, never silently changes day.
    try:
        tz = ZoneInfo(settings.user_timezone)
    except (ValueError, KeyError):
        raise GoalProgrammeError("programme_timezone_invalid") from None
    return _aware(now).astimezone(tz)


async def owner_delivery_policy(db, owner_id, now):
    """Conservative union of canonical Goal quiet windows and observer gates."""
    from src.goals.repository import deserialize_admission_budget
    from src.observer.manager import context_manager
    from src.scheduler.scheduled_jobs import _procedure_quiet_now
    preference = await db.get(ProgrammeNotificationPreference, owner_id)
    if not preference:
        return True, 0
    context = context_manager.get_context(owner_principal_id=preference.recipient_principal_id,
        owner_session_id=preference.recipient_root_id)
    quiet = context.interruption_mode in {"focus", "silent"} or context.user_state in {"winding_down", "deep_work", "in_meeting", "away"} or context.attention_budget_remaining <= 0
    allowance = 0
    goals = list((await db.execute(select(Goal))).scalars())
    for goal in goals:
        programmes = [p for p in _load(goal)["generations"] if p["owner_identity_id"] == owner_id]
        if not programmes:
            continue
        try:
            budget = deserialize_admission_budget(goal)
            if goal.admission_budget_json and budget is None:
                return True, 0
            if budget:
                quiet = quiet or _procedure_quiet_now(budget, now)
        except (ValueError, RuntimeError):
            return True, 0
        for programme in programmes:
            if (programme["state"] == "active" and _aware(datetime.fromisoformat(programme["expires_at"])) > now
                and goal.revision == programme["goal_revision"]
                and str(getattr(goal.status, "value", goal.status)) == "active"
                and goal.owner_session_id == programme["issuer_root_id"]
                and goal.owner_principal_id == programme["issuer_principal_id"]):
                allowance = max(allowance, programme["notification_limits"]["per_day"])
    return quiet, min(2, allowance)


def deterministic_deadlines(witness, brief, now):
    """Only explicit ISO deadlines in a physically reopened, cited source span.

    Findings/model urgency are never parsed as dates or categories. Category
    matching happens later against the operator's exact reviewed preference.
    """
    snapshots = {f"source:{a['slot']}": a["parsed"] for a in witness.artifacts.values() if a["kind"] == "snapshot"}
    results = []
    for finding in brief["findings"]:
        for citation in finding["citations"]:
            snapshot = snapshots.get(citation["source_id"])
            if not snapshot or not timedelta(0) <= now - _aware(snapshot.fetched_at) <= timedelta(hours=48):
                continue
            start, end = citation["first_line"], citation["last_line"]
            span = "\n".join(snapshot.lines[start - 1:end])
            if hashlib.sha256(span.encode()).hexdigest() != citation["span_sha256"]:
                continue
            for match in re.finditer(r"(?im)\b(?:deadline|due)\s*:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2}))\b", span):
                try:
                    due = _aware(datetime.fromisoformat(match[1].replace("Z", "+00:00")))
                except ValueError:
                    continue
                if timedelta(0) < due - now <= timedelta(hours=48):
                    # Store the literal source line solely as untrusted matching
                    # data, not notification text or authority.
                    results.append({"due_at": due.isoformat(), "source_id": citation["source_id"],
                        "span_sha256": citation["span_sha256"], "source_line": span.lower()[:4096]})
    return results[:16]


def finding_id(job_id: str, index: int) -> str:
    return hashlib.sha256(f"{job_id}:{index}".encode()).hexdigest()


def intent(row):
    return FollowThroughIntent(finding_id=row.finding_id, desired_outcome=row.desired_outcome,
        task_proposal_id=row.task_proposal_id, due_at=_aware(row.due_at) if row.due_at else None,
        status=row.status).model_dump(mode="json")


async def owner_identity(db, operator):
    live = await authenticate_principal(operator.principal.principal_id, db=db)
    if live.session_id != operator.session_id or live.ownership_continuity != "stable":
        raise GoalProgrammeError("programme_owner_recovery_required")
    root = await db.get(OperatorSession, live.session_id)
    identity = await db.get(OperatorIdentity, root.operator_identity_id) if root else None
    if identity is None or identity.revoked_at is not None:
        raise GoalProgrammeError("programme_identity_required")
    return root, identity


def run_outcome(run):
    return next((entry["payload"] for entry in json.loads(run.checkpoint_receipts_json)
        if entry.get("checkpoint_id") == "discovery:outcome"), None)


async def tick(now=None):
    """Coalesce only today's 08:00 occurrence, with durable stable-owner key."""
    goal_programme_service._ready()
    now = _aware(now or datetime.now(timezone.utc))
    local = local_clock(now)
    if local.hour < 8:
        return
    from src.guardian.goal_discovery import DISCOVERY_KIND
    from src.guardian.goal_discovery import goal_discovery_service
    from src.work_board.research_parent import discovery_authority
    from src.workflows.research_sources import physical_discovery_inputs
    from src.workflows.research_guard import discovery_writer_scope, assert_discovery_authority
    from src.goals.contracts import GoalProgramme, GoalProgrammeAuthorityBinding
    from uuid import uuid5, NAMESPACE_URL
    occurrence_day = now.astimezone(timezone.utc).date().isoformat()
    def occurrence_id(programme, original_day=occurrence_day):
        return "goal-discovery:" + uuid5(NAMESPACE_URL,
            f"seraph:public-discovery:{programme['owner_identity_id']}:{programme['id']}:{original_day}").hex
    # Stage physical immutable bytes without any SQLite writer held. The final
    # writer then checks the same original run/authority/checkpoint receipts.
    async with database.get_session() as db:
        goals = list((await db.execute(select(Goal).order_by(Goal.id))).scalars())
        finalized = set((await db.execute(select(ProgrammeDigestReceipt.owner_identity_id).where(
            ProgrammeDigestReceipt.local_date == local.date().isoformat(),
            ProgrammeDigestReceipt.phase == "finalized"))).scalars())
        pending_rows = list((await db.execute(select(ProgrammeDigestReceipt).where(
            ProgrammeDigestReceipt.local_date == local.date().isoformat(), ProgrammeDigestReceipt.phase == "pending"))).scalars())
        pending_days = {row.owner_identity_id: _aware(row.created_at).astimezone(timezone.utc).date().isoformat() for row in pending_rows}
        pending_originals = {row.owner_identity_id: set(ProgrammeDigestV1.model_validate_json(row.digest_json).programme_ids)
            for row in pending_rows}
        shortlist = {}
        for goal in goals:
            for programme in _load(goal)["generations"][-1:]:
                if programme["owner_identity_id"] not in finalized:
                    shortlist.setdefault(programme["owner_identity_id"], []).append((goal, programme))
        runs = {}
        eligible = set()
        async with discovery_writer_scope() as policy:
            for owner_id, programmes in shortlist.items():
                for goal, programme in programmes[:128]:
                    if owner_id in pending_originals and programme["id"] not in pending_originals[owner_id]:
                        continue
                    # This is the original UTC source occurrence, never an
                    # older success selected around a current held/Unknown job.
                    run = await db.scalar(select(WorkflowRunState).where(
                        WorkflowRunState.run_identity == occurrence_id(programme, pending_days.get(owner_id, occurrence_day)),
                        WorkflowRunState.job_kind == DISCOVERY_KIND))
                    runs[programme["id"]] = run
                    try:
                        await goal_programme_service.validate_current_binding(db=db,
                            binding=GoalProgrammeAuthorityBinding.from_programme(GoalProgramme.model_validate(programme),
                                "guardian.goal-discovery.v1"), policy=policy)
                    except GoalProgrammeError:
                        continue
                    eligible.add(programme["id"])
    staged = {}
    for programme_id, run in runs.items():
        if programme_id not in eligible or run is None or run.status not in {"succeeded", "degraded"}:
            continue
        binding = discovery_authority(run.declared_authority_json).programme_binding
        outcome = run_outcome(run)
        if not outcome:
            continue
        try:
            witness = await physical_discovery_inputs(goal_discovery_service.jobs, run.run_identity, completed_read=True)
            brief = next(a for a in witness.artifacts.values() if a["kind"] == "brief")
            if brief["reference"].model_dump(mode="json") != outcome["artifact_ref"]:
                raise ValueError("programme_digest_original_output_changed")
            staged[binding.programme_id] = (run, witness, brief["parsed"])
        except (ValueError, PermissionError, RuntimeError):
            staged[binding.programme_id] = None
    async with discovery_writer_scope() as policy:
      async with database.get_session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        owners = {}
        for original_goal in goals:
            goal = await db.get(Goal, original_goal.id, populate_existing=True)
            if goal is None:
                continue
            generations = _load(goal)["generations"]
            for programme in generations[-1:]:
                identity = await db.get(OperatorIdentity, programme["owner_identity_id"])
                if identity is not None and identity.revoked_at is None:
                    owners.setdefault(identity.id, []).append((goal, programme))
        for pending_receipt in (await db.execute(select(ProgrammeDigestReceipt).where(
                ProgrammeDigestReceipt.local_date == local.date().isoformat(),
                ProgrammeDigestReceipt.phase == "pending"))).scalars():
            owners.setdefault(pending_receipt.owner_identity_id, [])
        for owner_id, programmes in owners.items():
            existing = await db.scalar(select(ProgrammeDigestReceipt).where(
                ProgrammeDigestReceipt.owner_identity_id == owner_id,
                ProgrammeDigestReceipt.local_date == local.date().isoformat()))
            if existing is not None and existing.phase == "finalized":
                continue
            previous = await db.scalar(select(ProgrammeDigestReceipt).where(
                ProgrammeDigestReceipt.owner_identity_id == owner_id).order_by(
                    ProgrammeDigestReceipt.created_at.desc()).limit(1))
            if previous and existing is None:
                previous_local = _aware(previous.created_at).astimezone(ZoneInfo(previous.timezone))
                next_morning = (previous_local + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
                if now < next_morning.astimezone(timezone.utc) or local.date().isoformat() < previous.local_date:
                    continue  # Changing timezone/backwards clock cannot renew today's slot.
            ids, bindings, outputs, blocked = [], [], [], []
            if len(programmes) > 128:
                blocked.append("programme_digest_capacity_requires_review")
            original_ids = set(ProgrammeDigestV1.model_validate_json(existing.digest_json).programme_ids) if existing else None
            pending = False
            deadline = _aware(existing.finalize_deadline) if existing else min(now + timedelta(seconds=300),
                (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc))
            for goal, programme in programmes[:128]:
                if original_ids is not None and programme["id"] not in original_ids:
                    continue
                ids.append(programme["id"])
                if programme["state"] != "active" or _aware(datetime.fromisoformat(programme["expires_at"])) <= now:
                    blocked.append(programme.get("reason_code") or "programme_review_due")
                    continue
                current_run = await db.scalar(select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == occurrence_id(programme,
                        _aware(existing.created_at).astimezone(timezone.utc).date().isoformat() if existing else occurrence_day),
                    WorkflowRunState.job_kind == DISCOVERY_KIND))
                if now < deadline and (current_run is None or current_run.status in {"accepted", "queued", "running"}):
                    pending = True
                    blocked.append("programme_current_output_pending")
                    continue
                if current_run is not None and current_run.status not in {"succeeded", "degraded"}:
                    blocked.append("programme_current_output_unresolved")
                    continue
                source = staged.get(programme["id"])
                if source is None:
                    blocked.append("programme_no_completed_output")
                    continue
                original, witness, brief = source
                if current_run is None or current_run.run_identity != original.run_identity:
                    blocked.append("programme_current_output_changed")
                    continue
                run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == original.run_identity))
                try:
                    async with discovery_writer_scope(witness=witness):
                        await assert_discovery_authority(db, run.declared_authority_json, run=run)
                except (GoalProgrammeError, ValueError, RuntimeError):
                    blocked.append("programme_output_requires_current_readback")
                    continue
                outcome = run_outcome(run)
                # Only retain canonical original identifiers. Physical content
                # and authority are reopened on display and action, outside writer.
                outputs.extend(brief["prepared_artifact_refs"])
                bindings.append({"goal_id": goal.id, "programme_id": programme["id"],
                    "goal_revision": run.goal_revision, "grant_revision": programme["grant_revision"],
                    "finding_count": len(brief["findings"]),
                    "deadline_evidence": [{k: d[k] for k in ("due_at", "source_id", "span_sha256")}
                        for d in deterministic_deadlines(witness, brief, now)],
                    "job_id": run.run_identity, "artifact_ref": outcome["artifact_ref"]})
            digest = ProgrammeDigestV1(local_date=existing.local_date if existing else local.date().isoformat(),
                timezone=existing.timezone if existing else local.tzinfo.key,
                programme_ids=ids, finding_ids=[finding_id(b["job_id"], i) for b in bindings for i in range(b["finding_count"])],
                prepared_outputs=outputs, blocked_reasons=list(dict.fromkeys(blocked)))
            if original_ids and set(ids) != original_ids:
                digest = digest.model_copy(update={"programme_ids": sorted(original_ids),
                    "blocked_reasons": digest.blocked_reasons + ["programme_source_removed"]})
            receipt = existing or ProgrammeDigestReceipt(owner_identity_id=owner_id, local_date=digest.local_date,
                timezone=digest.timezone, digest_json=digest.model_dump_json(), finalize_deadline=deadline, created_at=now)
            receipt.digest_json, receipt.finding_bindings_json = digest.model_dump_json(), json.dumps(bindings)
            receipt.phase = "pending" if pending else "finalized"
            db.add(receipt)
    await deliver_notices(now)


async def _read_binding(operator, binding, now):
    from src.guardian.goal_discovery import goal_discovery_service
    result = await goal_discovery_service.read_brief(operator=operator, goal_id=binding["goal_id"],
        programme_id=binding["programme_id"], job_id=binding["job_id"])
    if result["artifact_ref"] != binding["artifact_ref"]:
        raise ValueError("programme_digest_original_output_changed")
    sources = result["brief"]["coverage"]["sources"]
    fresh = bool(sources) and all(timedelta(0) <= now - _aware(datetime.fromisoformat(source["fetched_at"])) <= timedelta(hours=48) for source in sources)
    return result, fresh


async def list_digests(operator, now=None):
    goal_programme_service._ready()
    now = _aware(now or datetime.now(timezone.utc))
    async with database.get_session() as db:
        _, identity = await owner_identity(db, operator)
        receipts = list((await db.execute(select(ProgrammeDigestReceipt).where(
            ProgrammeDigestReceipt.owner_identity_id == identity.id).order_by(
                ProgrammeDigestReceipt.created_at.desc()).limit(14))).scalars())
        follow_rows = list((await db.execute(select(ProgrammeFollowThrough).where(
            ProgrammeFollowThrough.owner_identity_id == identity.id))).scalars())
        visible_follow_rows = []
        for row in follow_rows:
            try:
                await authorize_disposition_replay(db, operator, identity, row)
            except GoalProgrammeError:
                continue
            visible_follow_rows.append(row)
    follows = {row.finding_id: row for row in visible_follow_rows}
    digests = []
    for receipt in receipts:
        digest = ProgrammeDigestV1.model_validate_json(receipt.digest_json)
        findings, errors, output_groups = [], [], []
        visible_programme_ids = set()
        async with database.get_session() as db:
            for goal in (await db.execute(select(Goal))).scalars():
                try:
                    await authorize_goal_read(db, operator, goal.id)
                except GoalProgrammeError:
                    continue
                visible_programme_ids.update(p["id"] for p in _load(goal)["generations"]
                    if p["owner_identity_id"] == identity.id and p["id"] in digest.programme_ids)
        for binding in json.loads(receipt.finding_bindings_json):
            async with database.get_session() as db:
                try:
                    await authorize_goal_read(db, operator, binding["goal_id"])
                except GoalProgrammeError:
                    errors.append("programme_read_scope_required")
                    continue
            visible_programme_ids.add(binding["programme_id"])
            try:
                readback, fresh = await _read_binding(operator, binding, now)
            except (GoalProgrammeError, ValueError, PermissionError, RuntimeError):
                errors.append("programme_output_requires_current_readback")
                for index in range(binding.get("finding_count", 0)):
                    identifier = finding_id(binding["job_id"], index)
                    row = follows.get(identifier)
                    findings.append({"id": identifier, **{k: binding[k] for k in ("goal_id", "programme_id", "job_id")},
                        "goal_revision": binding["goal_revision"], "grant_revision": binding["grant_revision"],
                        "text": "Original finding requires current source and authority readback.", "citations": [],
                        "prepared_outputs": [], "follow_through": intent(row) if row else None,
                        "task_id": row.task_proposal_id if row else None, "actionable": False,
                        "source_freshness": "blocked", "recovery": "Review the original Goal, programme and source artifacts; provider replay is forbidden."})
                continue
            output_groups.append((binding["goal_id"], readback["brief"]["prepared_artifact_refs"]))
            async with database.get_session() as db:
                goal = await db.get(Goal, binding["goal_id"])
                current_owner = bool(goal and goal.owner_principal_id == operator.principal.principal_id and goal.owner_session_id == operator.session_id)
            for index, finding in enumerate(readback["brief"]["findings"]):
                identifier = finding_id(binding["job_id"], index)
                row = follows.get(identifier)
                completed_outputs = await completed_task_outputs(operator, row) if row and row.task_proposal_id else []
                if completed_outputs:
                    row.status = "completed"
                elif row and row.status == "completed":
                    row.status = "blocked"
                findings.append({"id": identifier, **{k: binding[k] for k in ("goal_id", "programme_id", "job_id")},
                    "goal_revision": binding["goal_revision"], "grant_revision": binding["grant_revision"],
                    "text": finding["text"], "citations": finding["citations"],
                    "prepared_outputs": readback["brief"]["prepared_artifact_refs"] + completed_outputs,
                    "follow_through": intent(row) if row else None,
                    "task_id": row.task_proposal_id if row else None,
                    "actionable": current_owner and fresh and (row is None or row.task_proposal_id is None and row.status != "dismissed") and (row is None or row.due_at is None or _aware(row.due_at) <= now),
                    "source_freshness": "current" if fresh else "stale",
                    "recovery": "Verify the original task's physical output before treating its linked output as complete." if row and row.status == "blocked" else "Selected recovery permits read-only inspection. Prepare new work only with current Goal ownership." if not current_owner else None if fresh else "Refresh sources in the original finite programme before preparing work."})
        # Recheck after asynchronous physical readbacks: an earlier permission
        # snapshot cannot expose private fallback metadata after scope changes.
        allowed_goal_ids, final_programme_ids = set(), set()
        async with database.get_session() as db:
            await owner_identity(db, operator)
            for goal in (await db.execute(select(Goal))).scalars():
                try:
                    await authorize_goal_read(db, operator, goal.id)
                except GoalProgrammeError:
                    continue
                allowed_goal_ids.add(goal.id)
                final_programme_ids.update(p["id"] for p in _load(goal)["generations"]
                    if p["owner_identity_id"] == identity.id and p["id"] in digest.programme_ids)
            for binding in json.loads(receipt.finding_bindings_json):
                if binding["goal_id"] in allowed_goal_ids:
                    final_programme_ids.add(binding["programme_id"])
        findings = [f for f in findings if f["goal_id"] in allowed_goal_ids]
        visible_outputs = [output for goal_id, outputs in output_groups
            if goal_id in allowed_goal_ids for output in outputs]
        visible_programme_ids &= final_programme_ids
        private_scope_denied = set(digest.programme_ids) - visible_programme_ids
        if not visible_programme_ids and digest.programme_ids:
            continue  # No current readable original Goal: omit historical existence too.
        projected = digest.model_copy(update={"programme_ids": [p for p in digest.programme_ids if p in visible_programme_ids],
            "finding_ids": [f["id"] for f in findings], "prepared_outputs": visible_outputs,
            "blocked_reasons": list(dict.fromkeys((digest.blocked_reasons if not private_scope_denied else ["programme_read_scope_required"]) + errors +
                (["programme_digest_original_day_elapsed"] if receipt.phase == "pending" and now.astimezone(ZoneInfo(receipt.timezone)).date().isoformat() > receipt.local_date else [])))})
        digests.append({"id": receipt.id, "digest": projected.model_dump(mode="json"),
            "findings": findings, "created_at": _aware(receipt.created_at).isoformat()})
    programmes = await programme_status(operator, now)
    return {"digests": digests, "programmes": programmes, "notifications": await notification_status(operator, now)}


async def programme_status(operator, now):
    from src.guardian.goal_discovery import goal_discovery_service
    async with database.get_session() as db:
        _, identity = await owner_identity(db, operator)
        goals = list((await db.execute(select(Goal))).scalars())
    result = []
    for goal in goals:
        if not any(p["owner_identity_id"] == identity.id for p in _load(goal)["generations"]):
            continue
        try:
            inspected = await goal_programme_service.inspect(operator=operator, goal_id=goal.id)
            history = await goal_discovery_service.inspect(operator=operator, goal_id=goal.id)
        except GoalProgrammeError:
            continue  # Unselected recovery scopes do not expose another Root's Goal.
        for programme in inspected["programmes"]:
            runs = [r for r in history["runs"] if r["programme_id"] == programme["id"]]
            latest = runs[0] if runs else None
            async with database.get_session() as db:
                run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == latest["job_id"])) if latest else None
                reservations = list((await db.execute(select(InferenceCostReservation).where(
                    InferenceCostReservation.job_id.in_([r["job_id"] for r in runs])))).scalars()) if runs else []
            local = local_clock(now)
            next_at = local.replace(hour=8, minute=0, second=0, microsecond=0)
            if local >= next_at:
                next_at += timedelta(days=1)
            completed = next((r for r in runs if r["status"] in {"succeeded", "degraded"} and r["outcome"]), None)
            last_run, sources_checked, output = None, 0, None
            recovery = programme["recovery"]
            if completed:
                async with database.get_session() as db:
                    completed_run = await db.scalar(select(WorkflowRunState).where(
                        WorkflowRunState.run_identity == completed["job_id"]))
                try:
                    if completed_run is None or completed_run.finished_at is None:
                        raise ValueError("programme_completed_timestamp_missing")
                    readback = await goal_discovery_service.read_brief(operator=operator, goal_id=goal.id,
                        programme_id=programme["id"], job_id=completed["job_id"])
                    last_run = _aware(completed_run.finished_at).isoformat()
                    sources_checked = len(readback["brief"]["coverage"]["sources"])
                    output = readback["artifact_ref"]["artifact_id"]
                except (GoalProgrammeError, ValueError, PermissionError, RuntimeError):
                    recovery = "Last completed source output requires current physical and authority readback."
            async with database.get_session() as db:
                await owner_identity(db, operator)
                try:
                    await authorize_goal_read(db, operator, goal.id)
                except GoalProgrammeError:
                    continue
            result.append({"goal_id": goal.id, "id": programme["id"], "grant_revision": programme["grant_revision"],
                "state": programme["state"], "reason_code": programme["reason_code"],
                "last_run": last_run,
                "sources_checked": sources_checked,
                "output": output,
                "current_run_status": run.status if run else None,
                "current_admitted_at": _aware(run.started_at).isoformat() if run else None,
                # Existing discovery admission has a UTC occurrence and native
                # outstanding hold; a guessed timer is not execution truth.
                "next_run": None,
                "next_digest_at": next_at.isoformat() if programme["state"] == "active" and next_at < datetime.fromisoformat(programme["expires_at"]) else None,
                "remaining_allowance_microusd": max(0, programme["budget"]["max_inference_microusd"] - sum(
                    (r.actual_cost_microusd or 0 if r.state == "settled" else r.bound_microusd if r.state != "released" else 0) for r in reservations)),
                "recovery": recovery})
    return result


async def preferences(operator, request):
    async with database.get_session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        root, identity = await owner_identity(db, operator)
        row = await db.get(ProgrammeNotificationPreference, identity.id)
        if row is None:
            row = ProgrammeNotificationPreference(owner_identity_id=identity.id,
                recipient_principal_id=root.principal_id, recipient_root_id=root.id)
        row.recipient_principal_id, row.recipient_root_id = root.principal_id, root.id
        row.enabled, row.deadline_categories_json = request.enabled, json.dumps(request.deadline_categories)
        row.updated_at = datetime.now(timezone.utc)
        db.add(row)
    return await notification_status(operator)


async def notification_status(operator, now=None):
    now = _aware(now or datetime.now(timezone.utc))
    async with database.get_session() as db:
        _, identity = await owner_identity(db, operator)
        preference = await db.get(ProgrammeNotificationPreference, identity.id)
        receipt = await db.scalar(select(ProgrammeDigestReceipt).where(
            ProgrammeDigestReceipt.owner_identity_id == identity.id,
            ProgrammeDigestReceipt.local_date == local_clock(now).date().isoformat()))
        quiet, _ = await owner_delivery_policy(db, identity.id, now)
        unknown = await db.scalar(select(NativeNotificationOutbox.id).where(
            NativeNotificationOutbox.idempotency_key.in_([f"programme-{kind}:{identity.id}:{local_clock(now).date().isoformat()}" for kind in ("digest", "deadline")]),
            NativeNotificationOutbox.status == "unknown"))
    return {"enabled": bool(preference and preference.enabled),
        "deadline_categories": json.loads(preference.deadline_categories_json) if preference else [],
        "digest_slots_remaining": int(receipt is None or receipt.digest_notice == "unreserved"),
        "deadline_slots_remaining": int(receipt is None or receipt.deadline_notice == "unreserved"),
        "quiet_hours_active": quiet,
        "delivery_debt": bool(unknown or receipt and "unknown" in {receipt.digest_notice, receipt.deadline_notice})}


async def _deadline_negative_key(db, receipt, preference, now, policy):
    """Bounded negative-only memo identity; this never authorizes a notice."""
    from src.goals.contracts import GoalProgramme, GoalProgrammeAuthorityBinding
    bindings = json.loads(receipt.finding_bindings_json)
    if not bindings or len(bindings) > 128:
        return None
    root = await db.get(OperatorSession, preference.recipient_root_id, populate_existing=True)
    identity = await db.get(OperatorIdentity, receipt.owner_identity_id, populate_existing=True)
    if (not preference.enabled or not json.loads(preference.deadline_categories_json)
        or not identity or identity.revoked_at or not root or root.revoked_at or root.is_bearer_tombstone
        or root.operator_identity_id != identity.id or root.principal_id != preference.recipient_principal_id
        or _aware(root.idle_expires_at) <= now or _aware(root.absolute_expires_at) <= now):
        return None
    current = []
    for binding in bindings:
        goal = await db.get(Goal, binding["goal_id"], populate_existing=True)
        generation = next((p for p in _load(goal)["generations"] if p["id"] == binding["programme_id"]), None) if goal else None
        if (not generation or goal.revision != binding["goal_revision"]
            or generation["grant_revision"] != binding["grant_revision"] or generation["owner_identity_id"] != identity.id):
            return None
        try:
            await goal_programme_service.validate_current_binding(db=db,
                binding=GoalProgrammeAuthorityBinding.from_programme(GoalProgramme.model_validate(generation),
                    "guardian.goal-discovery.v1"), policy=policy)
        except GoalProgrammeError:
            return None
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding["job_id"]))
        if run is None or run.status not in {"succeeded", "degraded"}:
            return None
        current.append({"goal_revision": goal.revision, "goal_owner": [goal.owner_principal_id, goal.owner_session_id],
            "programme": generation, "run": {name: getattr(run, name) for name in (
                "run_identity", "job_kind", "status", "revision", "goal_id", "goal_revision", "plan_revision",
                "owner_principal_id", "operator_session_id", "input_digest", "authority_digest", "budget_digest",
                "declared_authority_json", "checkpoint_receipts_json", "artifact_receipts_json", "effect_receipts_json",
                "artifact_paths_json", "result_digest")}})
    clean = [{k: v for k, v in binding.items() if k != "deadline_no_match"} for binding in bindings]
    # Authentication touch/idle extension is not a source/category change.
    # Current expiry/revocation is checked above on every pass, never cached.
    value = {"preferences": preference.model_dump(mode="json"), "root": {
        "id": root.id, "principal_id": root.principal_id, "operator_identity_id": root.operator_identity_id,
        "token_hash": root.token_hash, "absolute_expires_at": _aware(root.absolute_expires_at).isoformat()},
        "identity_id": identity.id, "digest": receipt.digest_json,
        "bindings": clean, "current": current}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def deliver_notices(now):
    from src.observer.native_notification_queue import NativeNotificationQueue
    from src.approval.runtime import set_runtime_context, reset_runtime_context
    from src.guardian.goal_discovery import goal_discovery_service
    from src.workflows.research_sources import physical_discovery_inputs
    from src.workflows.research_guard import discovery_writer_scope, assert_discovery_authority
    from src.goals.contracts import GoalProgramme, GoalProgrammeAuthorityBinding
    async with database.get_session() as db:
        possible = list((await db.execute(select(ProgrammeDigestReceipt).where(
            ProgrammeDigestReceipt.local_date == local_clock(now).date().isoformat(),
            ProgrammeDigestReceipt.phase == "finalized", ProgrammeDigestReceipt.deadline_notice == "unreserved"))).scalars())
        candidates, staged_keys = [], {}
        async with discovery_writer_scope() as policy:
            for candidate in possible:
                preference = await db.get(ProgrammeNotificationPreference, candidate.owner_identity_id)
                if not preference or not preference.enabled or not json.loads(preference.deadline_categories_json):
                    continue
                identity = await db.get(OperatorIdentity, candidate.owner_identity_id)
                root = await db.get(OperatorSession, preference.recipient_root_id)
                if (not identity or identity.revoked_at or root is None or root.is_bearer_tombstone or root.revoked_at
                    or root.operator_identity_id != identity.id or root.principal_id != preference.recipient_principal_id
                    or _aware(root.idle_expires_at) <= now or _aware(root.absolute_expires_at) <= now):
                    continue
                quiet, allowance = await owner_delivery_policy(db, identity.id, now)
                if quiet or allowance < 2:
                    continue
                original_ids = set(ProgrammeDigestV1.model_validate_json(candidate.digest_json).programme_ids)
                current_ids = set()
                for goal in (await db.execute(select(Goal))).scalars():
                    for raw in _load(goal)["generations"]:
                        if raw["id"] not in original_ids or raw["owner_identity_id"] != identity.id:
                            continue
                        try:
                            await goal_programme_service.validate_current_binding(db=db,
                                binding=GoalProgrammeAuthorityBinding.from_programme(GoalProgramme.model_validate(raw),
                                    "guardian.goal-discovery.v1"), policy=policy)
                        except GoalProgrammeError:
                            continue
                        current_ids.add(raw["id"])
                if current_ids != original_ids:
                    continue
                bindings = json.loads(candidate.finding_bindings_json)
                if not any(timedelta(0) < _aware(datetime.fromisoformat(d["due_at"])) - now <= timedelta(hours=48)
                    for binding in bindings for d in binding.get("deadline_evidence", [])):
                    continue
                valid = True
                for binding in bindings:
                    goal = await db.get(Goal, binding["goal_id"], populate_existing=True)
                    raw = next((p for p in _load(goal)["generations"] if p["id"] == binding["programme_id"]), None) if goal else None
                    if (raw is None or goal.revision != binding["goal_revision"]
                        or raw["grant_revision"] != binding["grant_revision"] or raw["owner_identity_id"] != identity.id):
                        valid = False
                        break
                    try:
                        await goal_programme_service.validate_current_binding(db=db,
                            binding=GoalProgrammeAuthorityBinding.from_programme(GoalProgramme.model_validate(raw),
                                "guardian.goal-discovery.v1"), policy=policy)
                    except GoalProgrammeError:
                        valid = False
                        break
                if valid:
                    key = await _deadline_negative_key(db, candidate, preference, now, policy)
                    try:
                        memo = DeadlineNegativeMemo.model_validate(bindings[0].get("deadline_no_match"))
                    except ValueError:
                        memo = None
                    if key is None or memo is not None and memo.key == key:
                        continue
                    staged_keys[candidate.id] = key
                    candidates.append(candidate)
    staged, failed_receipts = {}, set()
    for candidate in candidates:
        for binding in json.loads(candidate.finding_bindings_json):
            if not any(timedelta(0) < _aware(datetime.fromisoformat(d["due_at"])) - now <= timedelta(hours=48)
                for d in binding.get("deadline_evidence", [])):
                continue
            try:
                staged[binding["job_id"]] = await physical_discovery_inputs(goal_discovery_service.jobs, binding["job_id"], completed_read=True)
            except (ValueError, PermissionError, RuntimeError):
                failed_receipts.add(candidate.id)
                continue
    async with discovery_writer_scope() as policy:
      async with database.get_session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        rows = list((await db.execute(select(ProgrammeDigestReceipt).where(
            ProgrammeDigestReceipt.local_date == local_clock(now).date().isoformat(),
            ProgrammeDigestReceipt.phase == "finalized"))).scalars())
        selected = []
        for row in rows:
            preference = await db.get(ProgrammeNotificationPreference, row.owner_identity_id)
            identity = await db.get(OperatorIdentity, row.owner_identity_id)
            if not preference or not preference.enabled or not identity or identity.revoked_at:
                continue
            root = await db.get(OperatorSession, preference.recipient_root_id)
            if (root is None or root.is_bearer_tombstone or root.revoked_at is not None
                or root.operator_identity_id != identity.id or root.principal_id != preference.recipient_principal_id):
                continue
            quiet, allowance = await owner_delivery_policy(db, identity.id, now)
            if quiet or allowance < 1:
                continue
            # Slot and native outbox insert share this exact writer. No external
            # display occurs here. Actual ambiguous daemon delivery consumes it.
            if row.digest_notice == "unreserved":
                row.digest_notice = "unknown"
                selected.append((row, "digest", root.principal_id, root.id, None))
            if allowance >= 2 and row.deadline_notice == "unreserved":
                categories = json.loads(preference.deadline_categories_json)
                urgent, evaluated = False, 0
                bindings = json.loads(row.finding_bindings_json)
                expected = sum(any(timedelta(0) < _aware(datetime.fromisoformat(d["due_at"])) - now <= timedelta(hours=48)
                    for d in b.get("deadline_evidence", [])) for b in bindings)
                for binding in bindings:
                    witness = staged.get(binding["job_id"])
                    if witness is None:
                        continue
                    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding["job_id"]))
                    try:
                        async with discovery_writer_scope(witness=witness):
                            await assert_discovery_authority(db, run.declared_authority_json, run=run)
                    except (GoalProgrammeError, ValueError, RuntimeError):
                        continue
                    evaluated += 1
                    brief = next(a["parsed"] for a in witness.artifacts.values() if a["kind"] == "brief")
                    urgent = urgent or any(any(re.search(r"(?<!\w)" + re.escape(category) + r"(?!\w)", deadline["source_line"]) for category in categories)
                        for deadline in deterministic_deadlines(witness, brief, now))
                if not urgent and expected and (evaluated == expected or row.id in failed_receipts) and row.id in staged_keys:
                    current_key = await _deadline_negative_key(db, row, preference, now, policy)
                    if current_key == staged_keys[row.id]:
                        bindings[0]["deadline_no_match"] = DeadlineNegativeMemo(key=current_key,
                            reason="deadline_source_requires_current_readback" if row.id in failed_receipts
                            else "reviewed_category_no_match").model_dump()
                        row.finding_bindings_json = json.dumps(bindings)
                if urgent:
                    row.deadline_notice = "unknown"
                    selected.append((row, "deadline", root.principal_id, root.id,
                        hashlib.sha256(preference.deadline_categories_json.encode()).hexdigest()))
            db.add(row)
        for row, kind, principal_id, root_id, category_digest in selected:
            # Canonical recipient proof above grants only this generic native
            # notice; no synthetic Root or model/tool grant is manufactured.
            tokens = set_runtime_context(None, "high_risk", trust_principal=None)
            try:
                await NativeNotificationQueue(max_attempts=1).enqueue(intervention_id=None,
                    title="Programme digest ready" if kind == "digest" else "Cited programme deadline",
                    body="Review your daily programme findings in the Guardian Inbox.",
                    intervention_type="programme_" + kind, urgency=1,
                    owner_principal_id=principal_id, operator_session_id=root_id,
                    causation_id=row.id, _db=db,
                    correlation_id=category_digest,
                    idempotency_key=f"programme-{kind}:{row.owner_identity_id}:{row.local_date}")
                setattr(row, kind + "_notice", "enqueued")
                db.add(row)
            finally:
                reset_runtime_context(tokens)


async def notice_claim_reason(db, notification, now):
    """DB-only consent/revision/quiet fence in the existing daemon claim CAS."""
    row = await db.get(ProgrammeDigestReceipt, notification.causation_id)
    if row is None or row.phase != "finalized" or row.local_date != local_clock(now).date().isoformat():
        return "programme_notice_original_day_expired"
    kind = "deadline" if notification.intervention_type == "programme_deadline" else "digest"
    if (notification.idempotency_key != f"programme-{kind}:{row.owner_identity_id}:{row.local_date}"
        or getattr(row, kind + "_notice") != "enqueued" or notification.max_attempts != 1):
        return "programme_notice_lineage_invalid"
    identity = await db.get(OperatorIdentity, row.owner_identity_id)
    preference = await db.get(ProgrammeNotificationPreference, row.owner_identity_id)
    root = await db.get(OperatorSession, notification.operator_session_id)
    if not identity or identity.revoked_at or not preference or not preference.enabled:
        return "programme_notifications_disabled"
    if kind == "deadline" and (not json.loads(preference.deadline_categories_json)
        or notification.correlation_id != hashlib.sha256(preference.deadline_categories_json.encode()).hexdigest()):
        return "programme_deadline_categories_changed"
    if (root is None or root.is_bearer_tombstone or root.revoked_at or root.operator_identity_id != identity.id
        or root.principal_id != notification.owner_principal_id
        or preference.recipient_root_id != root.id or preference.recipient_principal_id != root.principal_id):
        return "programme_notice_recipient_revoked"
    quiet, allowance = await owner_delivery_policy(db, identity.id, now)
    if quiet:
        return "programme_quiet_hours"
    if allowance < (2 if notification.intervention_type == "programme_deadline" else 1):
        return "programme_notification_allowance_unavailable"
    original_ids = set(ProgrammeDigestV1.model_validate_json(row.digest_json).programme_ids)
    found_ids = set()
    for goal in (await db.execute(select(Goal))).scalars():
        for generation in _load(goal)["generations"]:
            if generation["id"] not in original_ids:
                continue
            found_ids.add(generation["id"])
            if (goal.revision != generation["goal_revision"] or generation["state"] != "active"
                or str(getattr(goal.status, "value", goal.status)) != "active"
                or generation["owner_identity_id"] != identity.id
                or _aware(datetime.fromisoformat(generation["expires_at"])) <= now):
                return "programme_notice_original_authority_changed"
    if found_ids != original_ids:
        return "programme_notice_original_authority_changed"
    for binding in json.loads(row.finding_bindings_json):
        goal = await db.get(Goal, binding["goal_id"], populate_existing=True)
        generation = next((p for p in _load(goal)["generations"] if p["id"] == binding["programme_id"]), None) if goal else None
        if (not goal or goal.revision != binding["goal_revision"] or not generation or generation["state"] != "active"
            or generation["grant_revision"] != binding["grant_revision"]
            or _aware(datetime.fromisoformat(generation["expires_at"])) <= now):
            return "programme_notice_original_authority_changed"
    if kind == "deadline" and not any(timedelta(0) < _aware(datetime.fromisoformat(d["due_at"])) - now <= timedelta(hours=48)
        for binding in json.loads(row.finding_bindings_json) for d in binding.get("deadline_evidence", [])):
        return "programme_deadline_elapsed"
    return None


async def authorize_goal_read(db, operator, goal_id):
    """Canonical current Goal read scope, never execution or data ownership alone."""
    from src.auth.ownership import selected_read_scopes, selected_read_principal
    goal = await db.get(Goal, goal_id, populate_existing=True)
    if goal is not None:
        if goal.owner_session_id == operator.session_id and goal.owner_principal_id == operator.principal.principal_id:
            return
        scopes = await selected_read_scopes(operator, "goal", db=db)
        if (scopes.get(goal.id) == goal.owner_session_id
            and await selected_read_principal(operator, "goal", goal.id, db=db) == goal.owner_principal_id):
            return
    raise GoalProgrammeError("programme_disposition_read_denied")


async def authorize_disposition_replay(db, operator, identity, receipt):
    """Private receipt reads use the original finding's canonical Goal scope.

    This metadata-only fence neither reopens sources nor refreshes authority.
    Deleted Goals retain receipts but confer no new historical read permission.
    """
    for digest_row in (await db.execute(select(ProgrammeDigestReceipt).where(
        ProgrammeDigestReceipt.owner_identity_id == identity.id))).scalars():
        for binding in json.loads(digest_row.finding_bindings_json):
            if not any(finding_id(binding["job_id"], index) == receipt.finding_id
                for index in range(binding["finding_count"])):
                continue
            return await authorize_goal_read(db, operator, binding["goal_id"])
    raise GoalProgrammeError("programme_disposition_read_denied")


async def action(operator, identifier, request, now=None):
    now = _aware(now or datetime.now(timezone.utc))
    staged_at = time.monotonic()
    if len(json.dumps({"finding_id": identifier, "request": request.model_dump(mode="json")}).encode()) > 16384:
        raise GoalProgrammeError("programme_action_receipt_capacity_requires_review")
    request_digest = hashlib.sha256(json.dumps({"finding_id": identifier,
        "request": request.model_dump(mode="json")}, sort_keys=True).encode()).hexdigest()
    async with database.get_session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        _, identity = await owner_identity(db, operator)
        replay = await db.scalar(select(ProgrammeFindingAction).where(
            ProgrammeFindingAction.owner_identity_id == identity.id,
            ProgrammeFindingAction.idempotency_key == request.idempotency_key))
        if replay:
            if replay.request_digest != request_digest:
                raise GoalProgrammeError("programme_action_idempotency_conflict")
            if not json.loads(replay.result_json).get("_pending"):
                await authorize_disposition_replay(db, operator, identity, replay)
                return json.loads(replay.result_json)
        used = await db.scalar(select(func.count(ProgrammeFindingAction.id)).where(
            ProgrammeFindingAction.owner_identity_id == identity.id,
            ProgrammeFindingAction.local_date == local_clock(now).date().isoformat()))
        if used >= 128 and replay is None:
            raise GoalProgrammeError("programme_action_capacity_requires_review")
    data = await list_digests(operator, now)
    finding = next((f for d in data["digests"] for f in d["findings"] if f["id"] == identifier), None)
    if finding is None:
        raise GoalProgrammeError("programme_finding_not_found")
    if not finding["actionable"]:
        raise GoalProgrammeError("programme_finding_refresh_required")
    if request.action == "snooze" and (request.until is None or _aware(request.until) <= now or _aware(request.until) > now + timedelta(days=30)):
        raise GoalProgrammeError("programme_followup_date_invalid")
    from src.guardian.goal_discovery import goal_discovery_service
    from src.workflows.research_sources import physical_discovery_inputs
    from src.workflows.research_guard import discovery_writer_scope, assert_discovery_authority
    witness = await physical_discovery_inputs(goal_discovery_service.jobs, finding["job_id"], completed_read=True)
    if witness.plan.goal_revision != finding["goal_revision"] or witness.plan.programme_id.hex != finding["programme_id"]:
        raise GoalProgrammeError("programme_finding_original_binding_changed")
    async def recheck_disposition(db):
        goal = await db.get(Goal, finding["goal_id"], populate_existing=True)
        if goal is None or goal.revision != finding["goal_revision"]:
            raise GoalProgrammeError("programme_goal_review_required")
        await goal_programme_service._issuer(db, operator, goal)
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == finding["job_id"]))
        if run is None:
            raise GoalProgrammeError("programme_finding_refresh_required")
        await assert_discovery_authority(db, run.declared_authority_json, run=run)
        observed = now + timedelta(seconds=time.monotonic() - staged_at)
        snapshots = [a["parsed"] for a in witness.artifacts.values() if a["kind"] == "snapshot"]
        if not snapshots or any(not timedelta(0) <= observed - _aware(s.fetched_at) <= timedelta(hours=48) for s in snapshots):
            raise GoalProgrammeError("programme_finding_refresh_required")
        current_follow = await db.scalar(select(ProgrammeFollowThrough).where(
            ProgrammeFollowThrough.owner_identity_id == identity.id,
            ProgrammeFollowThrough.finding_id == identifier))
        if current_follow and (current_follow.task_proposal_id or current_follow.status == "dismissed"
            or current_follow.due_at is not None and _aware(current_follow.due_at) > observed):
            raise GoalProgrammeError("programme_finding_refresh_required")
    # Reserve finite disposition-receipt capacity before preparing a C1 card.
    # A crash resumes the same original finding's native idempotency key; this
    # record owns no job/attempt, budget, effect, provider contact or execution.
    async with discovery_writer_scope(witness=witness), database.get_session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        _, identity = await owner_identity(db, operator)
        receipt = await db.scalar(select(ProgrammeFindingAction).where(
            ProgrammeFindingAction.owner_identity_id == identity.id,
            ProgrammeFindingAction.idempotency_key == request.idempotency_key))
        if receipt:
            if receipt.request_digest != request_digest:
                raise GoalProgrammeError("programme_action_idempotency_conflict")
            if not json.loads(receipt.result_json).get("_pending"):
                await authorize_disposition_replay(db, operator, identity, receipt)
                return json.loads(receipt.result_json)
        await recheck_disposition(db)
        if receipt is None:
            used = await db.scalar(select(func.count(ProgrammeFindingAction.id)).where(
                ProgrammeFindingAction.owner_identity_id == identity.id,
                ProgrammeFindingAction.local_date == local_clock(now).date().isoformat()))
            if used >= 128:
                raise GoalProgrammeError("programme_action_capacity_requires_review")
            db.add(ProgrammeFindingAction(owner_identity_id=identity.id, finding_id=identifier,
                idempotency_key=request.idempotency_key, request_digest=request_digest,
                local_date=local_clock(now).date().isoformat(), result_json='{"_pending":true}', created_at=now))
    async with database.get_session() as db:
        _, identity = await owner_identity(db, operator)
        row = await db.scalar(select(ProgrammeFollowThrough).where(ProgrammeFollowThrough.owner_identity_id == identity.id,
            ProgrammeFollowThrough.finding_id == identifier))
        existing_task_id = row.task_proposal_id if row else None
        if existing_task_id and request.action == "accept_followup" and request.desired_outcome and request.desired_outcome != row.desired_outcome:
            raise GoalProgrammeError("programme_action_idempotency_conflict")
    task_id = None
    if request.action == "accept_followup":
        if not finding["actionable"] and not existing_task_id:
            raise GoalProgrammeError("programme_finding_refresh_required")
        task_id = existing_task_id or await prepare_task(operator, finding, request)
    async with discovery_writer_scope(witness=witness), database.get_session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        _, identity = await owner_identity(db, operator)
        replay = await db.scalar(select(ProgrammeFindingAction).where(
            ProgrammeFindingAction.owner_identity_id == identity.id,
            ProgrammeFindingAction.idempotency_key == request.idempotency_key))
        if replay:
            if replay.request_digest != request_digest:
                raise GoalProgrammeError("programme_action_idempotency_conflict")
            if not json.loads(replay.result_json).get("_pending"):
                await authorize_disposition_replay(db, operator, identity, replay)
                return json.loads(replay.result_json)
        await recheck_disposition(db)
        row = await db.scalar(select(ProgrammeFollowThrough).where(ProgrammeFollowThrough.owner_identity_id == identity.id,
            ProgrammeFollowThrough.finding_id == identifier))
        if row is None:
            row = ProgrammeFollowThrough(owner_identity_id=identity.id, finding_id=identifier)
        row.desired_outcome = request.desired_outcome or row.desired_outcome or "Review the cited evidence and prepare a local checklist."
        if task_id:
            row.task_proposal_id, row.status = task_id, "prepared"
        else:
            row.status = "deferred" if request.action == "snooze" else "dismissed"
        row.due_at = request.until if request.action == "snooze" else row.due_at
        row.updated_at = now
        db.add(row)
        result = {"finding_id": identifier, "follow_through": intent(row), "task_id": row.task_proposal_id}
        if len(json.dumps(result).encode()) > 16384:
            raise GoalProgrammeError("programme_action_receipt_capacity_requires_review")
        replay.result_json = json.dumps(result, sort_keys=True)
        db.add(replay)
        return result


async def prepare_task(operator, finding, request):
    from src.work_board.dispatcher import _dispatcher
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec, PlanStep, TaskLimits, WorkBoardOwner
    service = _dispatcher.general_tasks
    if service is None:
        raise GoalProgrammeError("programme_task_service_unavailable")
    descriptors, tool_digest = service.snapshot()
    tool = next((d for d in descriptors if d.tool_id == "write_file"), None)
    if tool is None:
        raise GoalProgrammeError("programme_local_draft_tool_unavailable")
    from src.guardian.goal_discovery import goal_discovery_service
    from src.workflows.research_sources import physical_discovery_inputs
    from src.workflows.research_guard import discovery_writer_scope, assert_discovery_authority
    witness = await physical_discovery_inputs(goal_discovery_service.jobs, finding["job_id"], completed_read=True)
    if witness.plan.goal_revision != finding["goal_revision"] or witness.plan.programme_id.hex != finding["programme_id"]:
        raise GoalProgrammeError("programme_finding_original_binding_changed")
    async def publication_check(db):
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == finding["job_id"]))
        await assert_discovery_authority(db, run.declared_authority_json, run=run)
        goal = await db.get(Goal, finding["goal_id"], populate_existing=True)
        await goal_programme_service._issuer(db, operator, goal)
    # No content supplies paths, tool identities or new authority. The server
    # selects a private local output; the operator separately accepts this plan.
    content = "# Public finding follow-through\n\n" + (request.desired_outcome or "Review cited public evidence.") + "\n\nUntrusted finding:\n" + finding["text"] + "\n\nOriginal finding: " + finding["id"] + "\nCitations: " + json.dumps(finding["citations"], sort_keys=True) + "\n\n- [ ] Verify the original cited evidence.\n- [ ] Decide the next action separately.\n"
    plan = PlanSpec(revision=1, steps=[PlanStep(step_id="prepare_checklist", tool_id="write_file",
        input={"file_path": f"programme-followthrough/{finding['id']}.md", "content": content},
        output_contract=tool.output_schema)])
    task_request = GeneralTaskCreate(goal_revision=finding["goal_revision"], idempotency_key="finding:" + finding["id"],
        input=GeneralTaskInput(goal_ref=finding["goal_id"], intent="Prepare a local checklist for the selected public finding.",
            requested_output=tool.output_schema, tool_set_digest=tool_digest,
            limits=TaskLimits(max_steps=1, max_inference_calls=0, wall_seconds=300, max_cost_microusd=0,
                max_outstanding_children=1)),
        plan=plan, expected_plan_revision=1)
    async with database.get_session() as db:
        goal = await db.get(Goal, finding["goal_id"])
        if goal is None or goal.revision != finding["goal_revision"]:
            raise GoalProgrammeError("programme_goal_review_required")
        await goal_programme_service._issuer(db, operator, goal)
        await goal_programme_service.assert_authority(goal_id=goal.id, programme_id=finding["programme_id"],
            grant_revision=finding["grant_revision"], capability_id="guardian.goal-discovery.v1")
        mutation = await service.create(db, WorkBoardOwner(principal_id=operator.principal.principal_id,
            session_id=operator.session_id), task_request, publication_authority_check=publication_check,
            publication_authority_scope=lambda: discovery_writer_scope(witness=witness))
        return mutation.task.task_id


async def completed_task_outputs(operator, follow):
    """Link only current canonical Done output with an actual physical readback."""
    from src.work_board.dispatcher import _dispatcher
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.repository import BoardError
    from src.work_board.contracts import GeneralTaskEnvelope
    from src.work_board.dispatcher import _parse_typed_input
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.workspace import canonical_workspace_root
    service = _dispatcher.general_tasks
    if service is None:
        return []
    async with database.get_session() as db:
        try:
            references = await service.evidence(db, WorkBoardOwner(principal_id=operator.principal.principal_id,
                session_id=operator.session_id), ["board-output:" + follow.task_proposal_id])
        except BoardError:
            return []
        if not references:
            return []
        task = await service.repository.get_task(db, WorkBoardOwner(principal_id=operator.principal.principal_id,
            session_id=operator.session_id), follow.task_proposal_id)
        envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(task))
        output_path = f"programme-followthrough/{follow.finding_id}.md"
        step = next((s for s in envelope.plan.steps if s.tool_id == "write_file" and s.input.get("file_path") == output_path), None) if envelope.plan else None
        if step is None or not isinstance(step.input.get("content"), str):
            return []
        expected = step.input["content"].encode()
        try:
            actual = _safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / output_path,
                expected_digest=hashlib.sha256(expected).hexdigest(), expected_size=len(expected))
        except (BoardError, ValueError, OSError):
            return []
        stored = await db.get(ProgrammeFollowThrough, follow.id)
        if stored and stored.task_proposal_id == follow.task_proposal_id:
            stored.status = "completed"
            db.add(stored)
        return [{"artifact_id": "board-output:" + follow.task_proposal_id,
            "digest": hashlib.sha256(actual).hexdigest(), "schema_version": 1,
            "task_id": follow.task_proposal_id, "status": "completed"} for output in references]
