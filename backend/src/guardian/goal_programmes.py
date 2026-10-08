"""Finite, reviewed PUBLIC goal programmes under the current Python owners.

This owner stores reviewed authority, not jobs or accounting. Later programme
execution must use the native durable runtime and recheck this exact generation
before every contact and adoption. No model is called to manufacture consent.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import text

from src.auth.service import AuthenticatedOperator, authenticate_principal
from src.db import engine as database
from src.db.models import AuditEvent, Goal, OperatorIdentity, OperatorSession
from src.goals.contracts import GoalProgramme, GoalProgrammeAccept, GoalProgrammeRequest, GoalProgrammeAuthorityBinding
from src.model_fabric.effective_policy import current_inference_policy

CAPABILITY_IDS = ("guardian.goal-discovery.v1", "guardian.query-plan.v1", "guardian.public-search.v1",
                  "source.public-extract.v1", "guardian.prepare-brief.v1")
SERVICE_ID = "service:guardian-goal-programmes"
MAX_GENERATIONS = 128


class GoalProgrammeError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProgrammePolicySnapshot:
    """Policy staged by its owner before entering a native DB writer."""
    epoch: int
    digest: str
    blocked_reason: str | None = None


def stage_programme_policy() -> ProgrammePolicySnapshot:
    return ProgrammePolicySnapshot(*_policy_binding())


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


def _policy_binding() -> tuple[int, str, str | None]:
    try:
        configuration, digest = current_inference_policy()
        return configuration.egress_revision, digest, None
    except PermissionError:
        # No credentials or private settings enter the public preview.
        from src.model_fabric.configuration import read_model_fabric_configuration
        configuration = read_model_fabric_configuration()
        return configuration.egress_revision, _digest({"blocked": configuration.status,
            "revoked": configuration.egress_revoked, "epoch": configuration.egress_revision}), "provider_policy_unavailable"


def _load(goal: Goal) -> dict:
    raw = getattr(goal, "goal_programmes_json", None)
    if not raw:
        return {"revision": 0, "preview": None, "generations": []}
    try:
        stored = json.loads(raw)
        if (not isinstance(stored, dict) or set(stored) != {"revision", "preview", "generations"}
                or type(stored["revision"]) is not int or stored["revision"] < 0
                or not isinstance(stored["generations"], list) or len(stored["generations"]) > MAX_GENERATIONS):
            raise ValueError("invalid programme storage")
        stored["generations"] = [GoalProgramme.model_validate(item).model_dump(mode="json") for item in stored["generations"]]
        if stored["preview"] is not None:
            preview = stored["preview"]
            if not isinstance(preview, dict) or set(preview) != {"programme", "request"}:
                raise ValueError("invalid programme preview")
            GoalProgramme.model_validate(preview["programme"])
            GoalProgrammeRequest.model_validate(preview["request"])
        return stored
    except (ValueError, TypeError, KeyError):
        raise GoalProgrammeError("programme_storage_invalid") from None


def _save(goal: Goal, stored: dict) -> None:
    goal.goal_programmes_json = json.dumps(stored, sort_keys=True, separators=(",", ":"))


def _projection(programme: GoalProgramme) -> dict:
    # Public content is deliberate. Private goal text, identity and issuer
    # credentials are never copied into a reviewed public brief.
    result = programme.model_dump(mode="json", exclude={"owner_identity_id", "issuer_principal_id"})
    result["recovery"] = {
        "review_due": "Review a new finite programme; old attempts retain their original authority.",
        "paused": "Review a new programme revision before any new run.",
        "blocked": "Configure the governed route and finite budget, then review a new programme.",
        "revoked": "Authority is revoked; history and existing liabilities are retained.",
    }.get(programme.state)
    return result


class GoalProgrammeService:
    """Lifecycle-owned authority seam, with no import-time timer or activation."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._running = False

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False

    def _ready(self):
        if not self._running:
            raise GoalProgrammeError("programme_service_unavailable")

    async def _issuer(self, db, operator: AuthenticatedOperator, goal: Goal, *, require_goal_owner=True):
        # Reauthenticate against the canonical live Root at every mutation;
        # a caller-created AuthenticatedOperator cannot mint a finite grant.
        live = await authenticate_principal(operator.principal.principal_id, db=db)
        if live.session_id != operator.session_id or live.ownership_continuity != "stable":
            raise GoalProgrammeError("programme_owner_recovery_required")
        root = await db.get(OperatorSession, live.session_id)
        identity = await db.get(OperatorIdentity, root.operator_identity_id) if root and root.operator_identity_id else None
        if identity is None or identity.revoked_at is not None:
            raise GoalProgrammeError("programme_identity_required")
        if require_goal_owner and (goal.owner_principal_id != live.principal.principal_id or goal.owner_session_id != live.session_id):
            raise GoalProgrammeError("goal_owner_mismatch")
        return root, identity

    async def preview(self, *, operator, goal_id: str, request: GoalProgrammeRequest) -> dict:
        self._ready()
        # Resolve policy outside the short DB writer; configuration is
        # physically read from the current policy owner, never under SQL lock.
        epoch, route_digest, blocked = _policy_binding()
        now = _aware(self._clock())
        async with database.get_session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            goal = await db.get(Goal, goal_id)
            if goal is None:
                raise GoalProgrammeError("goal_not_found")
            root, identity = await self._issuer(db, operator, goal)
            stored = _load(goal)
            if goal.revision != request.expected_goal_revision:
                raise GoalProgrammeError("goal_revision_stale")
            if stored["revision"] != request.expected_grant_revision:
                raise GoalProgrammeError("programme_revision_stale")
            if str(getattr(goal.status, "value", goal.status)) != "active":
                raise GoalProgrammeError("goal_inactive")
            grant_revision = stored["revision"] + 1
            brief_digest = hashlib.sha256(request.public_brief.encode()).hexdigest()
            programme_id = _digest({"goal": goal.id, "revision": grant_revision, "brief": brief_digest})[:32]
            reviewed = {
                "id": programme_id, "goal_id": goal.id, "goal_revision": goal.revision,
                "grant_revision": grant_revision, "public_brief": request.public_brief, "brief_digest": brief_digest,
                "confirmed_at": now.isoformat(), "expires_at": (now + timedelta(days=request.duration_days)).isoformat(),
                "capability_ids": list(CAPABILITY_IDS), "budget": request.budget.model_dump(), "cadence": request.cadence,
                "notification_limits": request.notification_limits.model_dump(),
                "artifact_prefix": f"goal-programmes/{programme_id}/", "owner_identity_id": identity.id,
                "issuer_root_id": root.id, "issuer_principal_id": root.principal_id,
                "route_epoch": epoch, "route_digest": route_digest,
                "state": "blocked" if blocked or request.budget.max_inference_microusd == 0 else "active",
                "reason_code": "programme_zero_budget" if request.budget.max_inference_microusd == 0 else blocked,
            }
            reviewed["review_digest"] = _digest(reviewed)
            programme = GoalProgramme.model_validate(reviewed)
            paused_ids = []
            for old in stored["generations"]:
                if old["state"] in {"active", "blocked"} and old["brief_digest"] != brief_digest:
                    old["state"], old["reason_code"] = "paused", "programme_brief_review_required"
                    paused_ids.append(old["id"])
            stored["preview"] = {"programme": programme.model_dump(mode="json"), "request": request.model_dump(mode="json")}
            _save(goal, stored)
            db.add(goal)
            return {"programme": _projection(programme), "review_digest": programme.review_digest,
                "acknowledgments_required": ["public_web_acknowledged", "local_artifacts_acknowledged", "inference_ceiling_acknowledged"],
                "public_only": True, "preview_only": True, "paused_programme_ids": paused_ids}

    async def accept(self, *, operator, goal_id: str, request: GoalProgrammeAccept) -> dict:
        self._ready()
        epoch, route_digest, _ = _policy_binding()
        async with database.get_session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            goal = await db.get(Goal, goal_id)
            if goal is None:
                raise GoalProgrammeError("goal_not_found")
            root, identity = await self._issuer(db, operator, goal)
            stored = _load(goal)
            preview = stored["preview"]
            if preview is None:
                raise GoalProgrammeError("programme_preview_required")
            programme = GoalProgramme.model_validate(preview["programme"])
            requested = GoalProgrammeRequest.model_validate(request.model_dump(exclude={"review_digest",
                "public_web_acknowledged", "local_artifacts_acknowledged", "inference_ceiling_acknowledged"}))
            if (requested.model_dump(mode="json") != preview["request"] or request.review_digest != programme.review_digest
                    or programme.issuer_root_id != root.id or programme.owner_identity_id != identity.id):
                raise GoalProgrammeError("programme_review_stale")
            if goal.revision != programme.goal_revision or stored["revision"] != request.expected_grant_revision:
                raise GoalProgrammeError("programme_revision_stale")
            if epoch != programme.route_epoch or route_digest != programme.route_digest:
                raise GoalProgrammeError("programme_route_changed")
            if _aware(self._clock()) >= _aware(programme.expires_at):
                raise GoalProgrammeError("programme_preview_expired")
            if len(stored["generations"]) >= MAX_GENERATIONS:
                raise GoalProgrammeError("programme_history_capacity")
            for old in stored["generations"]:
                if old["state"] in {"active", "blocked"}:
                    old["state"], old["reason_code"] = "paused", "programme_superseded"
            stored["generations"].append(programme.model_dump(mode="json"))
            stored["revision"] = programme.grant_revision
            stored["preview"] = None
            _save(goal, stored)
            db.add(goal)
            db.add(AuditEvent(actor=root.principal_id, event_type="goal_programme_accepted", summary="Finite public programme reviewed",
                details_json=json.dumps({"goal_id": goal.id, "programme_id": programme.id, "grant_revision": programme.grant_revision,
                    "brief_digest": programme.brief_digest, "review_digest": programme.review_digest, "expires_at": programme.expires_at.isoformat()})))
            return _projection(programme)

    async def assert_authority(self, *, goal_id: str, programme_id: str, grant_revision: int, capability_id: str,
                               route_epoch: int | None = None) -> GoalProgramme:
        """Preflight snapshot ONLY; native CAS must validate_current_binding.

        Never treat this read-return as atomic permission to contact a provider
        or publish output. The existing job/effect owner owns those transitions.
        """
        self._ready()
        epoch, digest, policy_blocked = _policy_binding()
        async with database.get_session() as db:
            goal = await db.get(Goal, goal_id)
            if goal is None:
                raise GoalProgrammeError("goal_not_found")
            stored = _load(goal)
            raw = next((item for item in stored["generations"] if item["id"] == programme_id), None)
            if raw is None:
                raise GoalProgrammeError("programme_not_found")
            programme = GoalProgramme.model_validate(raw)
            root = await db.get(OperatorSession, programme.issuer_root_id)
            identity = await db.get(OperatorIdentity, programme.owner_identity_id)
            reason = self._reason(programme, goal, root, identity, epoch, digest, policy_blocked)
            if reason:
                raise GoalProgrammeError(reason)
            if programme.grant_revision != grant_revision or capability_id not in CAPABILITY_IDS or capability_id not in programme.capability_ids:
                raise GoalProgrammeError("programme_generation_or_capability_stale")
            if route_epoch is not None and programme.route_epoch != route_epoch:
                raise GoalProgrammeError("programme_route_changed")
            return programme

    async def preflight_binding(self, **kwargs) -> GoalProgrammeAuthorityBinding:
        programme = await self.assert_authority(**kwargs)
        return GoalProgrammeAuthorityBinding.from_programme(programme, kwargs["capability_id"])

    async def validate_current_binding(self, *, db, binding: GoalProgrammeAuthorityBinding,
                                       policy: ProgrammePolicySnapshot) -> GoalProgramme:
        """DB-ONLY validation within the existing native contact/adoption CAS.

        Caller owns the short writer transaction and the original durable
        attempt transition. Stage current policy and physical evidence outside
        that writer, under their existing owner fences; hold the existing
        configuration_mutation_lock across policy staging and CAS commit.
        Bind exact native job/attempt/lease/fence/effect-intent identities, or
        the original attempt plus staged output artifact reference/digest for
        adoption, in that owner's transition. This callable opens no
        session and performs no credential, filesystem or network operation.
        Its return is useful only when the caller commits its native CAS in
        this SAME transaction. Output must be staged before adoption validation.
        """
        self._ready()
        goal = await db.get(Goal, binding.goal_id, populate_existing=True)
        if goal is None:
            raise GoalProgrammeError("goal_not_found")
        raw = next((item for item in _load(goal)["generations"] if item["id"] == binding.programme_id), None)
        if raw is None:
            raise GoalProgrammeError("programme_not_found")
        programme = GoalProgramme.model_validate(raw)
        root = await db.get(OperatorSession, programme.issuer_root_id, populate_existing=True)
        identity = await db.get(OperatorIdentity, programme.owner_identity_id, populate_existing=True)
        reason = self._reason(programme, goal, root, identity, policy.epoch, policy.digest, policy.blocked_reason)
        if reason:
            raise GoalProgrammeError(reason)
        if (binding.capability_id not in CAPABILITY_IDS or binding.capability_id not in programme.capability_ids
                or GoalProgrammeAuthorityBinding.from_programme(programme, binding.capability_id) != binding):
            raise GoalProgrammeError("programme_binding_stale")
        return programme

    async def inspect(self, *, operator, goal_id: str) -> dict:
        self._ready()
        epoch, digest, blocked = _policy_binding()
        async with database.get_session() as db:
            goal = await db.get(Goal, goal_id)
            if goal is None:
                raise GoalProgrammeError("goal_not_found")
            current_root, identity = await self._issuer(db, operator, goal, require_goal_owner=False)
            if current_root.id != goal.owner_session_id:
                from src.auth.ownership import selected_read_scopes
                scopes = await selected_read_scopes(operator, "goal", db=db)
                if scopes.get(goal.id) != goal.owner_session_id:
                    raise GoalProgrammeError("programme_owner_recovery_required")
            stored = _load(goal)
            projections = []
            for raw in stored["generations"]:
                programme = GoalProgramme.model_validate(raw)
                if programme.owner_identity_id != identity.id:
                    raise GoalProgrammeError("programme_owner_mismatch")
                issuer = await db.get(OperatorSession, programme.issuer_root_id)
                reason = self._reason(programme, goal, issuer, identity, epoch, digest, blocked)
                if programme.state == "active" and reason:
                    # Passive state projection only: no timer, push, model
                    # request, grant renewal or new execution is produced.
                    programme = programme.model_copy(update={"state": "review_due" if reason == "programme_expired" else
                        "paused" if reason in {"programme_goal_review_required", "programme_route_changed"} else "blocked", "reason_code": reason})
                projections.append(_projection(programme))
            return {"goal_id": goal.id, "grant_revision": stored["revision"], "programmes": projections}

    async def control(self, *, operator, goal_id: str, programme_id: str, request, action: str) -> dict:
        self._ready()
        if action not in {"pause", "revoke"}:
            raise GoalProgrammeError("programme_control_invalid")
        async with database.get_session() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            goal = await db.get(Goal, goal_id)
            if goal is None:
                raise GoalProgrammeError("goal_not_found")
            root, identity = await self._issuer(db, operator, goal, require_goal_owner=False)
            stored = _load(goal)
            raw = next((item for item in stored["generations"] if item["id"] == programme_id), None)
            if raw is None:
                raise GoalProgrammeError("programme_not_found")
            programme = GoalProgramme.model_validate(raw)
            issuer = await db.get(OperatorSession, programme.issuer_root_id)
            if (programme.owner_identity_id != identity.id or issuer is None or issuer.is_bearer_tombstone
                    or issuer.operator_identity_id != identity.id or issuer.principal_id != programme.issuer_principal_id):
                raise GoalProgrammeError("programme_owner_mismatch")
            if root.id != programme.issuer_root_id and not request.recover_owner_acknowledged:
                raise GoalProgrammeError("programme_owner_recovery_required")
            if programme.grant_revision != request.expected_grant_revision:
                raise GoalProgrammeError("programme_revision_stale")
            if programme.state == "revoked" and action == "pause":
                raise GoalProgrammeError("programme_revoked")
            raw["state"] = "paused" if action == "pause" else "revoked"
            raw["reason_code"] = f"programme_{raw['state']}"
            stored["preview"] = None
            _save(goal, stored)
            db.add(goal)
            db.add(AuditEvent(actor=root.principal_id, event_type=f"goal_programme_{action}", summary="Finite public programme control",
                details_json=json.dumps({"goal_id": goal.id, "programme_id": programme.id, "grant_revision": programme.grant_revision,
                    "owner_recovery_acknowledged": bool(root.id != programme.issuer_root_id)})))
            return _projection(GoalProgramme.model_validate(raw))

    def _reason(self, programme, goal, root, identity, epoch, digest, blocked):
        if programme.state != "active":
            return programme.reason_code or f"programme_{programme.state}"
        if identity is None or identity.revoked_at is not None:
            return "programme_identity_revoked"
        if (root is None or root.is_bearer_tombstone or root.operator_identity_id != identity.id
                or root.principal_id != programme.issuer_principal_id):
            return "programme_issuer_unproved"
        # Browser logout/expiry never renews OR revokes this finite public
        # service grant. Identity and explicit programme revocation do.
        if (goal.revision != programme.goal_revision or goal.owner_principal_id != programme.issuer_principal_id
                or goal.owner_session_id != programme.issuer_root_id or str(getattr(goal.status, "value", goal.status)) != "active"):
            return "programme_goal_review_required"
        if _aware(self._clock()) >= _aware(programme.expires_at):
            return "programme_expired"
        if blocked:
            return blocked
        if epoch != programme.route_epoch or digest != programme.route_digest:
            return "programme_route_changed"
        if programme.budget.max_inference_microusd == 0:
            return "programme_zero_budget"
        return None


goal_programme_service = GoalProgrammeService()
