"""M2 canonical policy and opportunity authority; short SQLite writers only."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update, func

from src.db import engine as db_engine
from src.db.models import (AuditEvent, Goal, GuardianSourceWatch, OperatorIdentity, OperatorSession,
    GuardianOpportunity, GuardianDecisionPacket, GuardianSourceBaseline, WorkflowRunState, GuardianInboxDisposition)
from src.goals.repository import deserialize_admission_budget
from src.guardian.opportunity_contracts import GuardianPolicy, GuardianPolicySave, OpportunityError, digest, json_bytes

PENDING = ("queued", "assessing")


async def guard_notification_intent(db, *, intervention_id, owner, root_id, goal_id, goal_revision):
    """Reserve opportunity push against existing durable outbox intents only."""
    from src.db.models import GuardianIntervention, NativeNotificationOutbox
    intervention = await db.get(GuardianIntervention, intervention_id) if intervention_id else None
    opportunity = await db.get(GuardianOpportunity, intervention.opportunity_id) if intervention else None
    if (opportunity is None or intervention.intervention_type != "opportunity"
            or opportunity.status != "proposed" or opportunity.intervention_id != intervention.id
            or (opportunity.owner_principal_id, opportunity.original_root_id, opportunity.goal_id,
                opportunity.goal_revision) != (owner, root_id, goal_id, goal_revision)
            or (intervention.owner_principal_id, intervention.original_root_id, intervention.goal_id,
                intervention.goal_revision) != (owner, root_id, goal_id, goal_revision)):
        raise OpportunityError("opportunity_notification_lineage_invalid")
    _, _, _, policy, _ = await assert_opportunity_current(db, opportunity)
    if policy.max_notification_per_utc_day == 0:
        raise OpportunityError("opportunity_notifications_disabled")
    disposition = await db.get(GuardianInboxDisposition, opportunity.id)
    if disposition is None or disposition.state != "pending":
        raise OpportunityError("opportunity_notification_suppressed")
    current = now()
    day = current.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = list((await db.execute(select(NativeNotificationOutbox).where(
        NativeNotificationOutbox.intervention_type == "opportunity",
        NativeNotificationOutbox.owner_principal_id == owner,
        NativeNotificationOutbox.created_at >= day))).scalars().all())
    if (len(rows) >= 2 or sum(row.goal_id == goal_id for row in rows) >= min(1, policy.max_notification_per_utc_day)
            or any(utc(row.created_at) > current - timedelta(minutes=30) for row in rows)):
        raise OpportunityError("opportunity_notification_limit")


def _source_proof_mapping(raw, *, max_bytes=None):
    """Normalize only persisted proof JSON, object shape and canonical UTF-8."""
    try:
        if max_bytes is not None and (not isinstance(raw, str) or len(raw) > max_bytes
                or len(raw.encode("utf-8")) > max_bytes):
            raise OpportunityError("source_stale")
        value = json.loads(raw)
        if isinstance(value, dict):
            json_bytes(value)  # Validate encoding before source strings reach SQL or digest checks.
    except (ValueError, TypeError, RecursionError) as exc:
        raise OpportunityError("source_stale") from exc
    if not isinstance(value, dict):
        raise OpportunityError("source_stale")
    return value


async def assert_source_current(db, opportunity, *, evidence=None):
    """Check immutable packet binding and current source generation in SQL only."""
    from src.guardian.inbox import _job_has_verified_readbacks
    packet = await db.get(GuardianDecisionPacket, opportunity.source_packet_id)
    watch = await db.get(GuardianSourceWatch, opportunity.watch_id)
    if (packet is None or watch is None or watch.plan_revision != opportunity.watch_revision
            or packet.status not in {"succeeded", "degraded"} or packet.verification_status != "passed"
            or packet.plan_revision != opportunity.watch_revision
            or packet.goal_revision != opportunity.goal_revision):
        raise OpportunityError("source_stale")
    token = _source_proof_mapping(opportunity.source_token_json, max_bytes=16384)
    if (any(not isinstance(token.get(key), str) for key in ("checkpoint_sha256", "artifact_id",
            "source_set_digest", "criteria_digest", "read_authority_digest"))
            or not isinstance(token.get("sources"), list)
            or any(not isinstance(source, dict) or any(not isinstance(source.get(key), str)
                for key in ("source_key", "identity_digest", "target", "new_hash", "excerpt_sha256"))
                for source in token["sources"])):
        raise OpportunityError("source_stale")
    read_authority = _source_proof_mapping(watch.read_authority_json)
    if (packet.observed_checkpoint_sha256 != token["checkpoint_sha256"]
            or packet.opportunity_snapshot_artifact_id != token["artifact_id"]
            or packet.opportunity_snapshot_sha256 != opportunity.source_digest
            or watch.source_set_digest != token["source_set_digest"]
            or watch.criteria_digest != token["criteria_digest"]
            or digest(json_bytes(read_authority)) != token["read_authority_digest"]):
        raise OpportunityError("source_stale")
    run = (await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == packet.run_identity))).scalars().first()
    if not _job_has_verified_readbacks(run, packet=packet, watch=watch):
        raise OpportunityError("source_stale")
    for source in token["sources"]:
        baseline = (await db.execute(select(GuardianSourceBaseline).where(
            GuardianSourceBaseline.watch_id == watch.id,
            GuardianSourceBaseline.source_key == source["source_key"]))).scalars().first()
        if (baseline is None or baseline.state != "ready"
                or baseline.identity_digest != source["identity_digest"]
                or baseline.target != source["target"]
                or baseline.baseline_sha256 != source["new_hash"]):
            raise OpportunityError("source_stale")
    if evidence is not None and (str(evidence.packet_id) != packet.id
            or evidence.checkpoint_sha256 != token["checkpoint_sha256"]
            or evidence.watch_revision != opportunity.watch_revision
            or evidence.goal_revision != opportunity.goal_revision
            or [source.model_dump(exclude={"excerpt"}) for source in evidence.sources] != token["sources"]):
        raise OpportunityError("source_stale")
    return packet, watch


async def assert_opportunity_current(db, opportunity, *, evidence=None):
    result = await current_policy_authority(db, goal_id=opportunity.goal_id,
        owner=opportunity.owner_principal_id, root_id=opportunity.original_root_id,
        goal_revision=opportunity.goal_revision, policy_revision=opportunity.policy_revision,
        watch_id=opportunity.watch_id)
    if utc(opportunity.expires_at) <= now():
        raise OpportunityError("opportunity_expired")
    await assert_source_current(db, opportunity, evidence=evidence)
    return result


async def publish_verified_packet(event):
    identifier = await _publish_verified_packet(event)
    if identifier is None:
        return None
    # Publication and the contact marker share SQLite's writer lock. Close
    # obsolete queued or claimed transfers outside that writer.
    from src.guardian.opportunity_runtime import quiesce_opportunity
    async with db_engine.get_session() as db:
        latest = await db.get(GuardianOpportunity, identifier)
        rows = list((await db.execute(select(GuardianOpportunity).outerjoin(WorkflowRunState,
            GuardianOpportunity.job_id == WorkflowRunState.run_identity).where(
            GuardianOpportunity.watch_id == latest.watch_id,
            GuardianOpportunity.status == "silent", GuardianOpportunity.reason_code == "coalesced",
            GuardianOpportunity.job_id.is_not(None),
            (WorkflowRunState.run_identity.is_(None) | WorkflowRunState.status.in_(("queued", "running", "blocked"))))
            .order_by(GuardianOpportunity.created_at, GuardianOpportunity.id).limit(20))).scalars().all())
    for row in rows:
        await quiesce_opportunity(row, coalesced=True)
    return identifier


async def _stage_optional_preference(db, owner, **scope):
    from src.guardian.opportunity_preferences import stage_preference_use
    from src.work_board.repository import BoardError
    try:
        return await stage_preference_use(db, owner=owner, action="suppress_watch", **scope)
    except (BoardError, ValueError, OSError):
        # Unavailable preference proof never hides an ordinary candidate.
        return None


async def _recheck_optional_preference(db, witness):
    from src.guardian.opportunity_preferences import recheck_preference_use
    from src.work_board.repository import BoardError
    try:
        return await recheck_preference_use(db, witness=witness)
    except (BoardError, ValueError, OSError):
        return {"status": "blocked", "reason_code": "opportunity_preference_unavailable"}


async def _publish_verified_packet(event):
    """Only the successful live publication seam calls this typed event handler.

    Physical snapshot readback precedes the writer; its exact SQL binding is
    checked again under the lock. Neither restart recovery nor old packets
    synthesize publication events.
    """
    from src.guardian.opportunity_contracts import VerifiedSourcePacket
    from src.guardian.opportunity_runtime import read_snapshot
    event = VerifiedSourcePacket.model_validate(event)
    async with db_engine.get_session() as db:
        packet = await db.get(GuardianDecisionPacket, str(event.packet_id))
        if packet is None:
            raise OpportunityError("source_stale")
        watch = await db.get(GuardianSourceWatch, packet.watch_id)
        goal = await db.get(Goal, packet.goal_id)
        published_policy = policy_for(goal)
        if (watch is None or published_policy is None
                or utc(packet.created_at) < utc(published_policy.confirmed_at)):
            return None
        reference, sha = packet.opportunity_snapshot_artifact_id, packet.opportunity_snapshot_sha256
        # This live verified publication is an optional candidate, never a
        # security/recovery read. Stage adopted custody before the SQL writer.
        from src.work_board.contracts import WorkBoardOwner
        preference_owner = WorkBoardOwner(principal_id=watch.owner_principal_id, session_id=watch.owner_session_id)
        preference_witness = await _stage_optional_preference(db, preference_owner,
            goal_id=goal.id, goal_revision=event.goal_revision,
            watch_id=watch.id, watch_revision=event.watch_revision)
    evidence = None
    snapshot_error = None
    try:
        evidence = read_snapshot(reference, sha)
    except OpportunityError as exc:
        snapshot_error = exc.code
    async with db_engine.get_session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        packet = await db.get(GuardianDecisionPacket, str(event.packet_id))
        watch = await db.get(GuardianSourceWatch, packet.watch_id)
        goal, root, budget, policy, expiry = await current_policy_authority(db,
            goal_id=packet.goal_id, owner=watch.owner_principal_id,
            root_id=watch.owner_session_id, goal_revision=event.goal_revision,
            watch_id=watch.id)
        if packet.plan_revision != event.watch_revision or watch.plan_revision != event.watch_revision:
            raise OpportunityError("source_stale")
        # Snapshot bytes include publication identity; semantic dedupe uses
        # the actual immutable offered sources independently of packet UUID.
        semantic = digest(json_bytes([item.model_dump() for item in evidence.sources])) if evidence else packet.observed_checkpoint_sha256
        key = digest(json_bytes([goal.owner_principal_id, goal.id, goal.revision,
            watch.id, watch.plan_revision, semantic, policy.schema_version]))
        existing = (await db.execute(select(GuardianOpportunity).where(
            GuardianOpportunity.owner_principal_id == goal.owner_principal_id,
            GuardianOpportunity.dedupe_key == key))).scalars().first()
        if existing is None and snapshot_error is None and preference_witness is not None:
            preference = await _recheck_optional_preference(db, preference_witness)
            from src.guardian.opportunity_preferences import suppress_optional_opportunity
            if suppress_optional_opportunity(preference, optional=True):
                receipt_id = "guardian-opportunity-suppressed:" + digest(json_bytes([
                    packet.id, preference["proposal_id"]]))
                if await db.get(AuditEvent, receipt_id) is None:
                    db.add(AuditEvent(id=receipt_id, actor=watch.owner_principal_id,
                        event_type="guardian_opportunity_preference_suppressed", details_json=json_bytes({
                            "reason_code": "opportunity_preference_current", "source_packet_id": packet.id,
                            "watch_id": watch.id, "watch_revision": watch.plan_revision,
                            "goal_id": goal.id, "goal_revision": goal.revision,
                            "proposal_id": preference["proposal_id"]}).decode()))
                return None
        token = {"artifact_id": reference, "checkpoint_sha256": packet.observed_checkpoint_sha256,
                 "source_set_digest": watch.source_set_digest, "criteria_digest": watch.criteria_digest,
                 "read_authority_digest": digest(json_bytes(json.loads(watch.read_authority_json))),
                 "sources": [item.model_dump(exclude={"excerpt"}) for item in evidence.sources] if evidence else []}
        admitted_at = now()
        expiry = min(expiry, utc(packet.created_at) + timedelta(days=7))
        opportunity = GuardianOpportunity(id=str(uuid.uuid4()), owner_principal_id=goal.owner_principal_id,
            original_root_id=goal.owner_session_id, goal_id=goal.id, goal_revision=goal.revision,
            policy_revision=goal.guardian_policy_revision, watch_id=watch.id,
            watch_revision=watch.plan_revision, source_packet_id=packet.id,
            source_digest=sha or packet.observed_checkpoint_sha256, source_token_json=json_bytes(token).decode(), dedupe_key=key,
            expires_at=expiry, assessment_deadline_at=min(
                admitted_at + timedelta(seconds=min(120, budget.max_runtime_seconds)), expiry),
            status="blocked" if snapshot_error else "queued", reason_code=snapshot_error)
        if not snapshot_error:
            await assert_source_current(db, opportunity, evidence=evidence)
            # A claim does not imply contact. Replace pending work until its
            # durable contact marker exists; contacted history survives.
            from src.db.models import InferenceCostReservation
            uncontacted_native = select(WorkflowRunState.run_identity).where(
                WorkflowRunState.status.in_(("queued", "running")))
            contacted_native = select(InferenceCostReservation.job_id).where(
                InferenceCostReservation.contact_started_at.is_not(None))
            await db.execute(update(GuardianOpportunity).where(
                GuardianOpportunity.watch_id == watch.id, GuardianOpportunity.status.in_(("queued", "assessing")),
                GuardianOpportunity.id != (existing.id if existing else opportunity.id),
                (GuardianOpportunity.job_id.is_(None) | GuardianOpportunity.job_id.in_(uncontacted_native)),
                (GuardianOpportunity.job_id.is_(None) | GuardianOpportunity.job_id.not_in(contacted_native)))
                .values(status="silent", reason_code="coalesced",
                    revision=GuardianOpportunity.revision + 1))
            # Startup may already have cleared the native lease. This exact
            # zero-reservation classification can replace pending history,
            # but never serves as proof that an unowned Task has closed.
            from src.guardian.opportunity_runtime import assert_recovered_uncontacted_binding
            recovered_rows = (await db.execute(select(GuardianOpportunity, WorkflowRunState).join(
                WorkflowRunState, GuardianOpportunity.job_id == WorkflowRunState.run_identity).where(
                GuardianOpportunity.watch_id == watch.id,
                GuardianOpportunity.status.in_(PENDING),
                GuardianOpportunity.id != (existing.id if existing else opportunity.id),
                WorkflowRunState.status == "blocked",
                WorkflowRunState.failure_reason == "stale_lease_requires_reconciliation").limit(20))).all()
            for historical, run in recovered_rows:
                if any(getattr(historical, field) != getattr(opportunity, field) for field in (
                        "owner_principal_id", "original_root_id", "goal_id", "goal_revision",
                        "policy_revision", "watch_revision")):
                    continue
                try:
                    await assert_recovered_uncontacted_binding(db, run, historical, retry=False)
                except OpportunityError:
                    continue
                historical.status, historical.reason_code = "silent", "coalesced"
                historical.revision += 1
                db.add(historical)
        # A repeated semantic packet must retain its original disposition,
        # while still superseding other uncontacted candidates from this watch.
        if existing:
            return existing.id
        if not snapshot_error:
            pending = list((await db.execute(select(GuardianOpportunity).where(
                GuardianOpportunity.status.in_(PENDING)))).scalars().all())
            if (any(row.goal_id == goal.id for row in pending)
                    or sum(row.owner_principal_id == goal.owner_principal_id for row in pending) >= 2
                    or len(pending) >= 16):
                opportunity.status, opportunity.reason_code = "blocked", "opportunity_capacity_exhausted"
        db.add(opportunity)
        await db.flush()
        db.add(GuardianInboxDisposition(id=opportunity.id, owner_principal_id=opportunity.owner_principal_id,
            owner_session_id=opportunity.original_root_id, source_kind="guardian_opportunity",
            source_id=opportunity.id, source_digest=opportunity.source_digest, goal_id=opportunity.goal_id,
            goal_revision=opportunity.goal_revision, watch_id=opportunity.watch_id,
            plan_revision=opportunity.watch_revision, expires_at=opportunity.expires_at,
            created_at=opportunity.created_at))
        return opportunity.id


def utc(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


def now():
    return datetime.now(timezone.utc)


def policy_for(goal):
    if not goal or not goal.guardian_policy_json:
        return None
    try:
        return GuardianPolicy.model_validate_json(goal.guardian_policy_json)
    except (ValueError, TypeError):
        return None


async def current_goal_authority(db, *, goal_id, owner, root_id, goal_revision, require_budget=True):
    """Pure canonical checks; stable data ownership cannot revive a Root."""
    root = await db.get(OperatorSession, root_id)
    goal = await db.get(Goal, goal_id)
    if (root is None or root.principal_id != owner or root.is_bearer_tombstone
            or root.revoked_at or root.replaced_by_id
            or utc(root.absolute_expires_at) <= now() or utc(root.idle_expires_at) <= now()):
        raise OpportunityError("original_root_unavailable", 403)
    if root.operator_identity_id:
        identity = await db.get(OperatorIdentity, root.operator_identity_id)
        if identity is None or identity.revoked_at:
            raise OpportunityError("original_root_unavailable", 403)
    if goal is None or goal.owner_principal_id != owner or goal.owner_session_id != root_id:
        raise OpportunityError("goal_owner_mismatch", 403)
    if goal.revision != goal_revision or (require_budget and str(getattr(goal.status, "value", goal.status)) != "active"):
        raise OpportunityError("goal_review_required")
    budget = deserialize_admission_budget(goal)
    if require_budget and (not goal.proactive_enabled or budget is None or not budget.reviewed_grant or not budget.grant_id
            or budget.period_expires_at is None or utc(budget.period_expires_at) <= now()
            or budget.period_started_at is not None and utc(budget.period_started_at) > now()):
        raise OpportunityError("goal_review_required")
    if require_budget:
        from src.guardian.source_watch import _goal_admission
        admitted, reason, _ = _goal_admission(goal)
        if not admitted:
            raise OpportunityError(reason)
    return goal, root, budget


async def current_policy_authority(db, *, goal_id, owner, root_id, goal_revision, policy_revision=None, watch_id=None):
    goal, root, budget = await current_goal_authority(
        db, goal_id=goal_id, owner=owner, root_id=root_id, goal_revision=goal_revision)
    policy = policy_for(goal)
    if (policy is None or not policy.assessment_enabled or policy.original_root_id != root_id
            or policy.goal_revision != goal.revision or policy.grant_id != budget.grant_id
            or utc(policy.confirmed_at) > now() or utc(policy.review_due_at) <= now()
            or policy_revision is not None and goal.guardian_policy_revision != policy_revision):
        raise OpportunityError("goal_review_required")
    if watch_id is not None:
        if watch_id not in {str(value) for value in policy.source_watch_ids}:
            raise OpportunityError("source_not_selected", 403)
        watch = await db.get(GuardianSourceWatch, watch_id)
        if (watch is None or watch.state != "active" or watch.goal_id != goal.id
                or watch.goal_revision != goal.revision or watch.owner_principal_id != owner
                or watch.owner_session_id != root_id):
            raise OpportunityError("source_stale")
        from src.guardian.source_watch import parse_sources
        try:
            sources = parse_sources(json.loads(watch.sources_json))
        except (ValueError, TypeError):
            raise OpportunityError("source_stale")
        if any(source.kind != "public_https_text" for source in sources):
            raise OpportunityError("source_private_input_excluded", 403)
        # Reuse the actual watch permission owner. Public-watch service
        # lifetime is its finite Goal grant; M2 additionally requires the live
        # original Root checked above and never borrows the service exception.
        from src.guardian.source_watch import source_watch_service, SourceWatchError
        _source_proof_mapping(watch.read_authority_json)
        try:
            await source_watch_service._assert_read_authority(watch, db=db)
        except SourceWatchError as exc:
            raise OpportunityError(exc.code, 403) from exc
    expiry = min(utc(root.absolute_expires_at), utc(root.idle_expires_at), utc(budget.period_expires_at), utc(policy.review_due_at))
    return goal, root, budget, policy, expiry


async def save_policy(*, operator, goal_id: str, request: GuardianPolicySave):
    owner, root_id = operator.principal.principal_id, operator.session_id
    receipt_id = "guardian-policy:" + digest(json_bytes([owner, root_id, goal_id, str(request.idempotency_key)]))
    request_digest = digest(json_bytes(request.model_dump(mode="json")))
    async with db_engine.get_session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        goal, root, budget = await current_goal_authority(
            db, goal_id=goal_id, owner=owner, root_id=root_id, goal_revision=request.expected_goal_revision,
            require_budget=request.policy.assessment_enabled)
        existing = await db.get(AuditEvent, receipt_id)
        if existing:
            receipt = json.loads(existing.details_json)
            if receipt["request_digest"] != request_digest:
                raise OpportunityError("policy_idempotency_conflict")
            return receipt["result"]
        if goal.guardian_policy_revision != request.expected_policy_revision:
            raise OpportunityError("guardian_policy_revision_stale")
        policy = request.policy
        if (policy.original_root_id != root_id or policy.goal_revision != goal.revision
                or policy.assessment_enabled and policy.grant_id != budget.grant_id):
            raise OpportunityError("guardian_policy_binding_mismatch", 403)
        if policy.auto_stage_plan and not request.acknowledge_auto_stage_plan:
            raise OpportunityError("auto_stage_acknowledgment_required", 422)
        if policy.max_notification_per_utc_day and not request.acknowledge_notifications:
            raise OpportunityError("notification_acknowledgment_required", 422)
        for watch_id in policy.source_watch_ids if policy.assessment_enabled else ():
            watch = await db.get(GuardianSourceWatch, str(watch_id))
            if (watch is None or watch.owner_principal_id != owner or watch.owner_session_id != root_id
                    or watch.goal_id != goal.id or watch.goal_revision != goal.revision or watch.state != "active"):
                raise OpportunityError("source_stale")
            from src.guardian.source_watch import parse_sources
            if any(source.kind != "public_https_text" for source in parse_sources(json.loads(watch.sources_json))):
                raise OpportunityError("source_private_input_excluded", 403)
        confirmed = now()
        expiry = min(confirmed + timedelta(days=7), utc(root.absolute_expires_at), utc(root.idle_expires_at))
        if policy.assessment_enabled:
            expiry = min(expiry, utc(budget.period_expires_at))
        review_due = min(utc(policy.review_due_at), expiry)
        if review_due <= confirmed:
            raise OpportunityError("goal_review_required")
        saved = policy.model_copy(update={"confirmed_at": confirmed, "review_due_at": review_due})
        # Validate again after applying server time; model_copy skips validators.
        saved = GuardianPolicy.model_validate(saved.model_dump())
        result = {"goal_revision": goal.revision, "guardian_policy_revision": goal.guardian_policy_revision + 1,
                  "guardian_policy": saved.model_dump(mode="json"), "assessment_state": "enabled" if saved.assessment_enabled else "disabled"}
        changed = await db.execute(update(Goal).where(Goal.id == goal.id, Goal.revision == goal.revision,
            Goal.guardian_policy_revision == request.expected_policy_revision).values(
                guardian_policy_json=saved.model_dump_json(), guardian_policy_revision=result["guardian_policy_revision"])
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise OpportunityError("guardian_policy_revision_stale")
        db.add(AuditEvent(id=receipt_id, actor=owner, event_type="guardian_policy_saved",
                         summary="Finite guardian policy explicitly reviewed; no operation admitted",
                         details_json=json_bytes({"request_digest": request_digest, "result": result}).decode()))
        return result


def policy_projection(goal):
    policy = policy_for(goal)
    return {"guardian_policy_revision": goal.guardian_policy_revision,
            "guardian_policy": policy.model_dump(mode="json") if policy else None,
            "guardian_assessment_state": ("disabled" if policy is None or not policy.assessment_enabled
                                          else "goal_review_required" if policy.goal_revision != goal.revision
                                          or policy.review_due_at <= now() else "enabled")}


async def project_item(db, row, disposition=None, *, detail=False, operator=None):
    """Literal history plus current availability; judgment never becomes authority."""
    from src.guardian.opportunity_contracts import OpportunityAssessment
    reason = row.reason_code
    policy_reason = None
    from src.db.models import NativeNotificationOutbox
    delivery = (await db.execute(select(NativeNotificationOutbox).where(
        NativeNotificationOutbox.intervention_id == row.intervention_id,
        NativeNotificationOutbox.intervention_type == "opportunity",
        NativeNotificationOutbox.owner_principal_id == row.owner_principal_id,
        NativeNotificationOutbox.operator_session_id == row.original_root_id)
        .order_by(NativeNotificationOutbox.created_at.desc()).limit(1))).scalars().first() if row.intervention_id else None
    current = True
    try:
        await assert_opportunity_current(db, row)
    except OpportunityError as exc:
        current, policy_reason = False, exc.code
    assessment = None
    if row.assessment_json:
        try:
            assessment = OpportunityAssessment.model_validate_json(row.assessment_json).model_dump(mode="json")
        except ValueError:
            current, policy_reason = False, "assessment_readback_mismatch"
    disposition_state = disposition.state if disposition else "pending"
    state = disposition_state if row.status == "proposed" else row.status
    if utc(row.expires_at) <= now() and row.status == "proposed":
        state = "expired"
    suppressed = (disposition is not None and disposition.state == "snoozed"
        and disposition.snoozed_until is not None and utc(disposition.snoozed_until) > now())
    allowed = ["accept_followup", "snooze", "dismiss"] if (current and row.status == "proposed"
        and disposition_state in {"pending", "snoozed"} and not suppressed) else []
    cancel_allowed = False
    live_goal = await db.get(Goal, row.goal_id)
    if live_goal is not None:
        try:
            await current_goal_authority(db, goal_id=row.goal_id, owner=row.owner_principal_id,
                root_id=row.original_root_id, goal_revision=live_goal.revision, require_budget=False)
            native = (await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.run_identity == row.job_id))).scalars().first() if row.job_id else None
            from src.workflows.job_runtime import DURABLE_JOB_TRANSITIONS
            native_cancellable = row.job_id is None or (native is not None
                and "cancelled" in DURABLE_JOB_TRANSITIONS.get(native.status, frozenset()))
            cancel_allowed = (native_cancellable and row.status in {"queued", "assessing", "blocked", "unknown"}
                and row.reason_code not in {"cancel_requested", "assessment_cancel_requested", "operator_cancelled"})
        except OpportunityError:
            pass
    from src.guardian.feedback import opportunity_feedback_summary
    item = {"id": disposition.id if disposition else row.id,
        "feedback_summary": await opportunity_feedback_summary(db, row),
        "revision": disposition.revision if disposition else row.revision,
        "state": state, "source_kind": "guardian_opportunity", "source_id": row.id,
        "source_digest": row.source_digest, "opportunity_id": row.id,
        "opportunity_revision": row.revision, "opportunity_status": row.status,
        "title": "Public evidence opportunity" if row.status == "proposed" else "Opportunity assessment history",
        "summary": assessment["summary"] if assessment else "No proposed intervention",
        "why_now": assessment["reason"] if assessment else reason or policy_reason or "Awaiting bounded assessment",
        "assessment": assessment, "reason_code": reason or policy_reason,
        "goal_id": row.goal_id, "goal_revision": row.goal_revision,
        "watch_id": row.watch_id, "plan_revision": row.watch_revision,
        "task_id": disposition.task_id if disposition else None,
        "expires_at": utc(row.expires_at).isoformat(),
        "snoozed_until": utc(disposition.snoozed_until).isoformat() if disposition and disposition.snoozed_until else None,
        "created_at": utc(row.created_at).isoformat(), "updated_at": utc(disposition.updated_at).isoformat() if disposition else utc(row.created_at).isoformat(),
        "evidence": {"dossier_artifact_id": None, "dossier_sha256": None, "task_artifact_id": None, "task_sha256": None},
        "evidence_refs": [], "evidence_status": "verified" if assessment and current else "unavailable",
        "verification_status": "passed" if assessment else "not_proposed", "memory_status": "no_learning",
        "delivery_status": delivery.status if delivery else "not_requested", "policy_reason": policy_reason, "allowed_actions": allowed,
        "cancel_allowed": cancel_allowed,
        "cancel_requested": row.reason_code in {"cancel_requested", "assessment_cancel_requested", "operator_cancelled"},
        "quiescent": row.status == "cancelled"}
    if row.status == "unknown" and item["cancel_requested"]:
        receipts = list((await db.execute(select(AuditEvent).where(AuditEvent.actor == row.owner_principal_id,
            AuditEvent.event_type == "guardian_opportunity_cancel_requested",
            AuditEvent.created_at >= row.created_at).order_by(AuditEvent.created_at.desc()).limit(20))).scalars().all())
        item["quiescent"] = any(json.loads(receipt.details_json).get("result", {}).get("opportunity_id") == row.id
            and json.loads(receipt.details_json).get("result", {}).get("quiescent") is True for receipt in receipts)
    if not current:
        item["recovery_action"] = "review_goal_and_watch"
    from src.guardian.opportunity_plans import get_plan_offer, get_plan_preview
    item["plan_offer"] = await get_plan_offer(db, row, operator=operator)
    if row.proposal_id:
        from src.db.models import WorkBoardProposal
        proposal = await db.get(WorkBoardProposal, row.proposal_id)
        if proposal is not None:
            try:
                item["plan_preview"] = await get_plan_preview(db, row, proposal)
            except (OpportunityError, ValueError, KeyError, TypeError):
                item["plan_preview"] = None
    if detail:
        from src.guardian.inbox import _load_action_history
        history, truncated = await _load_action_history(db, owner_principal_id=row.owner_principal_id,
            owner_session_id=row.original_root_id, item_id=row.id)
        item.update(evidence_previews=[], action_history=[], action_history_truncated=False, job=None,
            links={"source_watch": f"/api/capabilities/source-watches/{row.watch_id}", "packet": None,
                   "board_task": f"/api/work-board/tasks/{disposition.task_id}" if disposition and disposition.task_id else None})
        item.update(action_history=history, action_history_truncated=truncated)
        if current:
            from src.guardian.opportunity_runtime import read_snapshot
            token = json.loads(row.source_token_json)
            try:
                evidence = read_snapshot(token["artifact_id"], row.source_digest)
                item["evidence_previews"] = [{"artifact_id": token["artifact_id"],
                    "artifact_type": "guardian_public_evidence_snapshot", "file_path": token["artifact_id"],
                    "sha256": row.source_digest, "owner_session_id": row.original_root_id,
                    "workflow_run_id": row.job_id, "source_id": source.source_key,
                    "text": source.excerpt, "line_count": len(source.excerpt.split("\n"))}
                    for source in evidence.sources]
            except OpportunityError as exc:
                item.update(evidence_status="unavailable", allowed_actions=[], reason_code=reason or exc.code,
                            policy_reason=exc.code, recovery_action="review_goal_and_watch")
    return item


async def list_history(*, owner, root_id, goal_id=None, limit=20, cursor=None, operator=None):
    from sqlalchemy import and_, or_
    from src.guardian.inbox import _decode_cursor, _encode_cursor
    if not 1 <= limit <= 20:
        raise OpportunityError("invalid_limit", 422)
    position = _decode_cursor(cursor)
    async with db_engine.get_session() as db:
        query = select(GuardianOpportunity).where(GuardianOpportunity.owner_principal_id == owner,
            GuardianOpportunity.original_root_id == root_id)
        if goal_id:
            query = query.where(GuardianOpportunity.goal_id == goal_id)
        if position:
            query = query.where(or_(GuardianOpportunity.created_at > position[0], and_(
                GuardianOpportunity.created_at == position[0], GuardianOpportunity.id > position[1])))
        rows = list((await db.execute(query.order_by(GuardianOpportunity.created_at, GuardianOpportunity.id)
            .limit(limit + 1))).scalars().all())
        items = [await project_item(db, row, await db.get(GuardianInboxDisposition, row.id), operator=operator) for row in rows[:limit]]
    return {"items": items, "next_cursor": _encode_cursor(rows[limit-1].created_at, rows[limit-1].id)
        if len(rows) > limit else None}


async def apply_inbox_action(*, owner, root_id, item_id, action, expected_revision, idempotency_key, until, reason):
    from src.db.models import GuardianInboxAction, WorkBoardStatus
    from src.guardian.inbox import InboxError, _safe_action_reason
    from src.guardian.opportunity_runtime import read_snapshot
    from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate
    from src.work_board.repository import WorkBoardRepository
    from src.vault import redaction as vault_redaction
    payload_digest = digest(json_bytes([item_id, action, expected_revision, idempotency_key,
        utc(until).isoformat() if until else None, _safe_action_reason(reason)]))

    async def replay(db):
        existing = (await db.execute(select(GuardianInboxAction).where(
            GuardianInboxAction.owner_principal_id == owner, GuardianInboxAction.owner_session_id == root_id,
            GuardianInboxAction.idempotency_key == idempotency_key))).scalars().first()
        if existing:
            if existing.payload_digest != payload_digest:
                raise InboxError("idempotency_conflict", "The action key is bound to another request")
            return json.loads(existing.safe_result_json)
        return None

    async with db_engine.get_session() as db:
        original = await replay(db)
        if original:
            return original
        disposition = await db.get(GuardianInboxDisposition, item_id)
        row = await db.get(GuardianOpportunity, disposition.source_id) if disposition else None
        if (row is None or disposition.source_kind != "guardian_opportunity"
                or row.owner_principal_id != owner or row.original_root_id != root_id):
            raise InboxError("inbox_item_not_found", "The inbox item does not exist", status_code=404)
        token = json.loads(row.source_token_json)
    # Stage bytes/redaction outside the writer, then recheck SQL bindings.
    try:
        evidence = read_snapshot(token["artifact_id"], row.source_digest)
    except OpportunityError as exc:
        raise InboxError(exc.code, "The immutable source evidence is unavailable") from exc
    safe_reason = _safe_action_reason(reason)
    if safe_reason:
        safe_reason = await vault_redaction.redact_secrets_in_text(safe_reason, fail_closed=True)
        safe_reason = str(safe_reason)[:500]
    async with db_engine.get_session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        original = await replay(db)
        if original:
            return original
        disposition = await db.get(GuardianInboxDisposition, item_id)
        row = await db.get(GuardianOpportunity, disposition.source_id)
        if (row.owner_principal_id != owner or row.original_root_id != root_id
                or disposition.owner_principal_id != owner or disposition.owner_session_id != root_id):
            raise InboxError("inbox_item_not_found", "The inbox item does not exist", status_code=404)
        if disposition.revision != expected_revision:
            raise InboxError("inbox_revision_stale", "Refresh the inbox item", current_revision=disposition.revision)
        if row.status != "proposed":
            raise InboxError("opportunity_not_proposed", "This history has no proposed intervention")
        try:
            await assert_opportunity_current(db, row, evidence=evidence)
        except OpportunityError as exc:
            raise InboxError(exc.code, "Review the current Goal and watch", recovery_action="review_goal_and_watch") from exc
        if disposition.state not in {"pending", "snoozed"}:
            raise InboxError("inbox_item_not_actionable", "This item is already disposed")
        if disposition.snoozed_until and utc(disposition.snoozed_until) > now():
            raise InboxError("inbox_item_snoozed", "This item remains snoozed")
        task_id = None
        state = "dismissed" if action == "dismiss" else "snoozed" if action == "snooze" else "accepted"
        if action == "snooze":
            if until is None or not now() + timedelta(minutes=15) <= utc(until) <= min(now() + timedelta(days=7), utc(row.expires_at)):
                raise InboxError("invalid_snooze_window", "Snooze must fit the current review expiry", status_code=422)
            disposition.snoozed_until = utc(until)
        elif action == "dismiss":
            row.status, row.reason_code, row.revision = "dismissed", "operator_dismissed", row.revision + 1
            db.add(row)
        else:
            mutation = await WorkBoardRepository().create_task(db, WorkBoardOwner(principal_id=owner, session_id=root_id),
                WorkBoardTaskCreate(title="Review cited public opportunity", body=f"Review only; no capability executed. opportunity_id={row.id}; source_digest={row.source_digest}.",
                    goal_id=row.goal_id, goal_revision=row.goal_revision, status=WorkBoardStatus.triage,
                    idempotency_scope=f"guardian-inbox:{row.id}", idempotency_key="accept"))
            task_id = mutation.task.task_id
        receipt = GuardianInboxAction(owner_principal_id=owner, owner_session_id=root_id, item_id=item_id,
            idempotency_key=idempotency_key, payload_digest=payload_digest, action=action,
            prior_revision=disposition.revision, result_revision=disposition.revision + 1,
            task_id=task_id, safe_reason=safe_reason)
        result = {"id": item_id, "revision": disposition.revision + 1, "state": state,
            "task_id": task_id, "receipt_id": receipt.id, "recovery_action": "open_task" if task_id else None}
        receipt.safe_result_json = json_bytes(result).decode()
        disposition.state, disposition.revision, disposition.updated_at = state, disposition.revision + 1, now()
        disposition.task_id = task_id or disposition.task_id
        disposition.last_action_receipt_id = receipt.id
        db.add(receipt)
        db.add(disposition)
        return result


async def cancel_opportunity(*, operator, opportunity_id, request):
    from src.guardian.opportunity_runtime import quiesce_opportunity
    owner, root_id = operator.principal.principal_id, operator.session_id
    receipt_id = "opportunity-cancel:" + digest(json_bytes([owner, root_id, str(request.idempotency_key)]))
    request_digest = digest(json_bytes([opportunity_id, request.model_dump(mode="json")]))
    async with db_engine.get_session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        receipt = await db.get(AuditEvent, receipt_id)
        if receipt:
            old = json.loads(receipt.details_json)
            if old["request_digest"] != request_digest:
                raise OpportunityError("cancel_idempotency_conflict")
            return old["result"]
        row = await db.get(GuardianOpportunity, opportunity_id)
        if row is None or row.owner_principal_id != owner or row.original_root_id != root_id:
            raise OpportunityError("opportunity_not_found", 404)
        current_goal = await db.get(Goal, row.goal_id)
        await current_goal_authority(db, goal_id=row.goal_id, owner=owner, root_id=root_id,
            goal_revision=current_goal.revision if current_goal else row.goal_revision, require_budget=False)
        if row.revision != request.expected_opportunity_revision:
            raise OpportunityError("opportunity_revision_stale")
        if row.status not in {"queued", "assessing", "unknown", "blocked"}:
            raise OpportunityError("opportunity_not_cancellable")
        row.reason_code, row.revision = "cancel_requested", row.revision + 1
        db.add(row)
        result = {"opportunity_id": row.id, "revision": row.revision, "status": row.status,
            "reason_code": "cancel_requested", "cancel_requested": True, "quiescent": False}
        db.add(AuditEvent(id=receipt_id, actor=owner, event_type="guardian_opportunity_cancel_requested",
            summary="Operator requested cancellation; execution owner must confirm quiescence",
            details_json=json_bytes({"request_digest": request_digest, "result": result}).decode()))
    quiescent = await quiesce_opportunity(row)
    async with db_engine.get_session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        latest = await db.get(GuardianOpportunity, row.id)
        if quiescent and latest.status != "unknown":
            latest.status, latest.reason_code, latest.revision = "cancelled", "operator_cancelled", latest.revision + 1
            db.add(latest)
        result = {"opportunity_id": latest.id, "revision": latest.revision, "status": latest.status,
            "reason_code": latest.reason_code, "cancel_requested": True, "quiescent": quiescent}
        receipt = await db.get(AuditEvent, receipt_id)
        receipt.details_json = json_bytes({"request_digest": request_digest, "result": result}).decode()
        db.add(receipt)
        return result
