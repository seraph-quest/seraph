"""Owner-scoped Moltbook controls on the existing Vault and durable Work lane.

SQL writers perform only canonical row checks and CAS. Physical input, Vault
decryption, provider I/O and artifact readback are staged outside those writers.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

from sqlalchemy import text as sql_text
from sqlmodel import select

from config.settings import settings
from src.artifacts.registry import build_artifact_record
from src.db import engine
from src.db.models import ApprovalRequest, MoltbookConnection, OperatorSession, Secret, WorkflowRunState
from src.goals.repository import deserialize_admission_budget
from src.integrations.moltbook import (CAPABILITY, JOB_KIND, KEY, MoltbookAdapter,
    MoltbookError, canonical, digest, identifier, route, safe_comments, safe_content, text)
from src.tools.filesystem_tool import _read_workspace_text_bounded, _safe_resolve
from src.vault.crypto import decrypt, encrypt
from src.vault.repository import secret_binding_digest, vault_repository
from src.work_board.contracts import WorkBoardOwner
from src.work_board.input_artifacts import _write_payload
from src.work_board.repository import WorkBoardRepository, BoardError
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, durable_job_repository
from src.workspace import canonical_workspace_root

READS = frozenset({"inspect", "feed", "post", "comments", "community"})
WRITES = frozenset({"create_post", "create_comment"})
PREFIX = "artifacts/moltbook/"
STATE_ID = "moltbook:state"


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


async def writer(db):
    if db.get_bind().dialect.name == "sqlite":
        await db.execute(sql_text("BEGIN IMMEDIATE"))


async def root_current(db, principal, session):
    row = await db.scalar(select(OperatorSession).where(OperatorSession.id == session,
        OperatorSession.principal_id == principal, OperatorSession.revoked_at.is_(None),
        OperatorSession.replaced_by_id.is_(None), OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now(), OperatorSession.absolute_expires_at > now()))
    if row is None:
        raise MoltbookError("moltbook_original_root_inactive", status_code=403)
    return row


def state(run):
    try:
        receipts = json.loads(run.checkpoint_receipts_json or "[]")
        found = [item["payload"] for item in receipts if item.get("checkpoint_id") == STATE_ID]
        if len(found) != 1 or not isinstance(found[0], dict) or len(canonical(found[0])) > 8192:
            raise ValueError()
        return found[0]
    except (ValueError, TypeError, KeyError):
        raise MoltbookError("moltbook_checkpoint_invalid") from None


def save_state(run, value):
    if len(canonical(value)) > 8192:
        raise MoltbookError("moltbook_checkpoint_bound")
    run.checkpoint_receipts_json = canonical([{"checkpoint_id": STATE_ID,
        "state_digest": digest(value), "safe": True, "payload": value,
        "recorded_at": now().isoformat(), "fencing_token": run.fencing_token}]).decode()
    run.revision += 1
    run.updated_at = now()


def connection_view(row):
    if row is None:
        return {"configured": False, "mode": "disabled", "credential_is_consent": False,
            "remote_refresh": "explicit_finite_inspect_required", "no_learning": True}
    consent = json.loads(row.consent_json)
    return {"configured": True, "id": row.id, "revision": row.revision, "mode": row.mode,
        "account_id": row.account_id, "account_name": row.account_name,
        "active_job_id": row.active_job_id, "cooldown_until": utc(row.cooldown_until).isoformat() if row.cooldown_until is not None else None, "setup_request_key": row.setup_key,
        "consent": {key: consent.get(key) for key in ("actions", "expires_at", "goal_id", "goal_revision", "session", "request_key", "request")},
        "credential_is_consent": False, "remote_refresh": "explicit_finite_inspect_required", "no_learning": True}


class MoltbookService:
    def __init__(self, *, adapter=None, jobs=None):
        self.adapter = adapter or MoltbookAdapter()
        self.jobs = jobs or durable_job_repository
        self._active = {}

    async def connection(self, owner):
        async with engine.get_session() as db:
            await root_current(db, owner.principal_id, owner.session_id)
            row = await db.scalar(select(MoltbookConnection).where(MoltbookConnection.owner_principal_id == owner.principal_id))
            return connection_view(row)

    async def configure_from_vault(self, owner, *, vault_key, request_key, expected_revision=None):
        text(vault_key, 256)
        snapshot = await vault_repository.snapshot(vault_key, owner_principal_id=owner.principal_id)
        if snapshot is None: raise MoltbookError("moltbook_owner_vault_credential_required")
        return await self.configure(owner, api_key=snapshot.value, request_key=request_key,
            expected_revision=expected_revision)

    async def configure(self, owner, *, api_key, request_key, expected_revision=None):
        """Local owner configuration; no Goal or remote contact is implied."""
        if type(api_key) is not str or KEY.fullmatch(api_key) is None:
            raise MoltbookError("moltbook_credential_invalid", status_code=422)
        identifier(request_key)
        setup_digest = digest({"owner": owner.principal_id, "session": owner.session_id,
            "key_digest": digest(api_key.encode()), "expected_revision": expected_revision})
        encrypted = encrypt(api_key)  # key-file/decrypt operations never occur in the writer
        async with engine.get_session() as db:
            await writer(db)
            await root_current(db, owner.principal_id, owner.session_id)
            row = await db.scalar(select(MoltbookConnection).where(MoltbookConnection.owner_principal_id == owner.principal_id))
            if row and row.setup_key == request_key:
                if row.setup_digest != setup_digest:
                    raise MoltbookError("moltbook_setup_idempotency_conflict")
                return connection_view(row)
            if row and (expected_revision != row.revision or row.active_job_id is not None):
                raise MoltbookError("moltbook_connection_busy_or_revision_changed")
            if row is None:
                if expected_revision is not None:
                    raise MoltbookError("moltbook_connection_revision_changed")
                row = MoltbookConnection(owner_principal_id=owner.principal_id, owner_session_id=owner.session_id)
                row.vault_key = "moltbook." + row.id
                db.add(row)
            else:
                old = await db.scalar(select(Secret).where(Secret.key == row.vault_key,
                    Secret.owner_principal_id == owner.principal_id))
                if old:
                    old.revoked_at = now()
                    db.add(old)
                row.revision += 1
                row.vault_key = "moltbook." + row.id + "." + str(row.revision)
            secret = Secret(key=row.vault_key, owner_principal_id=owner.principal_id,
                encrypted_value=encrypted, description="Private Moltbook credential")
            db.add(secret)
            await db.flush()
            # Existing Vault provenance uses the loaded SQL timestamp form;
            # SQLite drops timezone metadata on storage. Bind the actual row,
            # preserving the established shared digest encoding.
            await db.refresh(secret)
            row.credential_binding = secret_binding_digest(secret)
            row.owner_session_id, row.mode, row.consent_json = owner.session_id, "pending_claim", "{}"
            row.account_id = row.account_name = ""
            row.setup_key, row.setup_digest, row.updated_at = request_key, setup_digest, now()
            db.add(row)
            return connection_view(row)

    async def consent(self, owner, *, request_key, expected_revision, goal_id, goal_revision, actions,
                      duration_seconds, personal_noncommercial, no_redistribution):
        identifier(request_key)
        if (personal_noncommercial is not True or no_redistribution is not True
            or type(duration_seconds) is not int or not 30 <= duration_seconds <= 3600
            or type(actions) is not list or not actions or len(actions) > 7
            or any(type(a) is not str or a not in READS | WRITES for a in actions)
            or len(set(actions)) != len(actions)):
            raise MoltbookError("moltbook_explicit_consent_invalid", status_code=422)
        request_digest = digest([owner.principal_id, owner.session_id, expected_revision, goal_id,
            goal_revision, sorted(actions), duration_seconds, personal_noncommercial, no_redistribution])
        async with engine.get_session() as db:
            await writer(db)
            root = await root_current(db, owner.principal_id, owner.session_id)
            goal = await WorkBoardRepository._validate_goal(db, owner, goal_id=goal_id, goal_revision=goal_revision)
            row = await db.scalar(select(MoltbookConnection).where(MoltbookConnection.owner_principal_id == owner.principal_id))
            if row is None:
                raise MoltbookError("moltbook_connection_busy_or_revision_changed")
            secret = await db.scalar(select(Secret).where(Secret.key == row.vault_key,
                Secret.owner_principal_id == owner.principal_id, Secret.revoked_at.is_(None)))
            if secret is None or secret_binding_digest(secret) != row.credential_binding:
                raise MoltbookError("moltbook_credential_changed")
            prior = json.loads(row.consent_json)
            if prior.get("request_key") == request_key:
                if (prior.get("request_digest") != request_digest or prior.get("session") != owner.session_id
                    or prior.get("credential_binding") != row.credential_binding
                    or prior.get("connection_revision") != row.revision):
                    raise MoltbookError("moltbook_consent_idempotency_conflict")
                if datetime.fromisoformat(prior["expires_at"]) <= now():
                    raise MoltbookError("moltbook_original_consent_expired")
                return connection_view(row)
            if row.revision != expected_revision or row.active_job_id:
                raise MoltbookError("moltbook_connection_busy_or_revision_changed")
            expiry = min(now() + timedelta(seconds=duration_seconds), utc(root.absolute_expires_at), utc(root.idle_expires_at))
            budget = deserialize_admission_budget(goal)
            for limit in (goal.due_date, budget.period_expires_at if budget else None):
                if limit is not None: expiry = min(expiry, utc(limit))
            if expiry <= now(): raise MoltbookError("moltbook_consent_window_expired")
            row.revision += 1
            row.owner_session_id = owner.session_id
            row.consent_json = canonical({"id": str(uuid.uuid4()), "session": owner.session_id,
                "goal_id": goal_id, "goal_revision": goal_revision, "actions": sorted(actions),
                "issued_at": now().isoformat(), "expires_at": expiry.isoformat(), "connection_revision": row.revision,
                "credential_binding": row.credential_binding, "personal_noncommercial": True,
                "no_redistribution": True, "request_key": request_key, "request_digest": request_digest,
                "request": {"request_key": request_key, "expected_revision": expected_revision,
                    "goal_id": goal_id, "goal_revision": goal_revision, "actions": actions,
                    "duration_seconds": duration_seconds, "personal_noncommercial": True,
                    "no_redistribution": True}}).decode()
            if row.mode == "disabled": row.mode = "pending_claim"
            db.add(row)
            return connection_view(row)

    async def disable(self, owner, *, expected_revision):
        async with engine.get_session() as db:
            await writer(db)
            await root_current(db, owner.principal_id, owner.session_id)
            row = await db.scalar(select(MoltbookConnection).where(MoltbookConnection.owner_principal_id == owner.principal_id))
            if row is None or row.revision != expected_revision:
                raise MoltbookError("moltbook_connection_revision_changed")
            row.revision += 1
            row.mode, row.consent_json = "disabled", "{}"
            row.updated_at = now()
            db.add(row)
            # Retain active capacity and all contacted uncertainty until the
            # actual worker closes and an exact canonical recovery settles it.
            return connection_view(row)

    async def current(self, db, owner, run, *, lease=None, settlement_only=False):
        await root_current(db, owner.principal_id, owner.session_id)
        if (run.owner_principal_id != owner.principal_id or run.operator_session_id != owner.session_id
            or run.job_kind != JOB_KIND or run.capability_version != "1" or utc(run.deadline_at) <= now()):
            raise MoltbookError("moltbook_original_job_binding_changed")
        if lease:
            self.jobs._assert_lease(run, owner=lease[0], fencing_token=lease[1])
            if run.status != "running": raise MoltbookError("moltbook_job_not_running")
        if not settlement_only and run.checkpoint_receipts_json not in (None, "", "[]") and state(run).get("cancel_request"):
            raise MoltbookError("moltbook_original_job_cancelled")
        goal = await WorkBoardRepository._validate_goal(db, owner, goal_id=run.goal_id, goal_revision=run.goal_revision)
        budget = deserialize_admission_budget(goal)
        for limit in (goal.due_date, budget.period_expires_at if budget else None):
            if limit is not None and utc(limit) <= now(): raise MoltbookError("moltbook_goal_window_expired")
        authority = json.loads(run.declared_authority_json)
        reference = PREFIX + digest(run.run_identity.encode()) + ".input.enc"
        expected_inputs = {"payload_ref": reference, "payload_digest": authority["payload_digest"], "no_learning": True}
        if (run.input_digest != digest(expected_inputs) or run.authority_digest != digest(authority)
            or authority.get("principal") != owner.principal_id or authority.get("session_id") != owner.session_id
            or authority.get("owner_kind") != "user" or authority.get("capability_id") != CAPABILITY
            or authority.get("no_learning") is not True
            or run.run_fingerprint != digest([authority["payload_digest"], authority, run.priority])):
            raise MoltbookError("moltbook_canonical_input_authority_changed")
        row = await db.get(MoltbookConnection, authority["connection_id"], populate_existing=True)
        if (row is None or row.owner_principal_id != owner.principal_id or row.mode == "disabled"
            or row.revision != authority["connection_revision"] or row.active_job_id != run.run_identity
            or row.active_payload_digest != authority["payload_digest"]
            or row.credential_binding != authority["vault_binding_digest"] or digest(json.loads(row.consent_json)) != authority["consent_digest"]):
            raise MoltbookError("moltbook_connection_authority_changed")
        if authority["operation"] in WRITES and (row.mode != "active" or row.account_id != authority["account_id"]
            or row.account_name != authority["account_name"]):
            raise MoltbookError("moltbook_original_account_changed")
        consent = json.loads(row.consent_json)
        if (consent.get("session") != owner.session_id or consent.get("goal_id") != run.goal_id
            or consent.get("goal_revision") != run.goal_revision or consent.get("connection_revision") != row.revision
            or datetime.fromisoformat(consent["expires_at"]) <= now()
            or consent.get("personal_noncommercial") is not True or consent.get("no_redistribution") is not True
            or authority["operation"] not in consent.get("actions", [])):
            raise MoltbookError("moltbook_current_consent_required")
        secret = await db.scalar(select(Secret).where(Secret.key == row.vault_key,
            Secret.owner_principal_id == owner.principal_id, Secret.revoked_at.is_(None)))
        if secret is None or secret_binding_digest(secret) != authority["vault_binding_digest"]:
            raise MoltbookError("moltbook_credential_changed")
        if not settlement_only and row.cooldown_until and utc(row.cooldown_until) > now():
            raise MoltbookError("moltbook_provider_cooldown")
        return row

    async def prepare_read(self, owner, **request):
        if request.get("operation") not in READS:
            raise MoltbookError("moltbook_read_operation_required", status_code=422)
        replay = await self.replayed_admission(owner, request)
        if replay is not None: return replay
        admitted = await self._prepare(owner, **request)
        return await self.snapshot(owner, admitted["job_id"])

    async def replayed_admission(self, owner, request):
        """Exact existing admission readback; never admission or execution."""
        identifier(request["request_key"])
        job_id = "moltbook:" + digest([owner.principal_id, owner.session_id, request["request_key"]])[:40]
        async with engine.get_session() as db:
            await root_current(db, owner.principal_id, owner.session_id)
            prior = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
            if prior is None: return None
        # The snapshot independently verifies the immutable encrypted input
        # outside SQL and is scoped to the exact active original Root.
        result = await self.snapshot(owner, job_id)
        expected = {key: value for key, value in request.items() if key != "priority"}
        if (result.get("admission_request") != expected or result["priority"] != request.get("priority", 50)):
            raise MoltbookError("moltbook_original_admission_request_conflict")
        return result

    async def retain_cooldown(self, db, owner, run, value, receipt, lease, *, settlement_only=True):
        """Settlement only: audit and account cooldown share the failed GET writer."""
        if receipt.get("http_status") != 429: return
        expiry = datetime.fromisoformat(receipt["cooldown_until"])
        # The transport observed this expiry once; finalizers and replay must
        # never renew it. Audit survives drift, account mutation does not.
        value["provider_cooldown_until"] = expiry.isoformat()
        try:
            row = await self.current(db, owner, run, lease=lease, settlement_only=settlement_only)
        except (MoltbookError, BoardError):
            return
        if row.cooldown_until is None or utc(row.cooldown_until) < expiry:
            row.cooldown_until, row.updated_at = expiry, now()
            db.add(row)

    async def _prepare(self, owner, *, operation, fields, request_key, goal_id, goal_revision,
                       expected_revision, priority=50, review=None):
        if operation not in READS | WRITES: raise MoltbookError("moltbook_operation_not_allowed", status_code=422)
        identifier(request_key)
        if type(priority) is not int or not 0 <= priority <= 100: raise MoltbookError("moltbook_priority_invalid", status_code=422)
        if operation == "inspect":
            if fields: raise MoltbookError("moltbook_inspect_shape_invalid", status_code=422)
        else: route(operation, fields)
        payload = {"operation": operation, "fields": fields, "no_learning": True}
        if operation in WRITES:
            if not isinstance(review, dict): raise MoltbookError("moltbook_exact_public_target_review_required")
            payload["review"] = review
        payload_digest = digest(payload)
        job_id = "moltbook:" + digest([owner.principal_id, owner.session_id, request_key])[:40]
        async with engine.get_session() as db:
            await writer(db)
            await root_current(db, owner.principal_id, owner.session_id)
            goal = await WorkBoardRepository._validate_goal(db, owner, goal_id=goal_id, goal_revision=goal_revision)
            row = await db.scalar(select(MoltbookConnection).where(MoltbookConnection.owner_principal_id == owner.principal_id))
            if row is None or row.revision != expected_revision or row.mode == "disabled":
                raise MoltbookError("moltbook_connection_revision_changed")
            if row.cooldown_until and utc(row.cooldown_until) > now():
                raise MoltbookError("moltbook_provider_cooldown")
            if operation in WRITES and (row.mode != "active" or not row.account_id or not row.account_name):
                raise MoltbookError("moltbook_claimed_account_inspect_required")
            consent = json.loads(row.consent_json)
            if (consent.get("session") != owner.session_id or consent.get("connection_revision") != row.revision
                or consent.get("goal_id") != goal_id or consent.get("goal_revision") != goal_revision
                or operation not in consent.get("actions", []) or datetime.fromisoformat(consent["expires_at"]) <= now()):
                raise MoltbookError("moltbook_current_consent_required")
            deadline = min(now()+timedelta(seconds=300 if operation in WRITES else 30), datetime.fromisoformat(consent["expires_at"]))
            for limit in (goal.due_date,):
                if limit is not None: deadline = min(deadline, utc(limit))
            if row.active_job_id is not None:
                if row.active_job_id != job_id or row.active_payload_digest != payload_digest:
                    raise MoltbookError("moltbook_connection_operation_outstanding")
                deadline = utc(row.active_deadline_at)
            else:
                prior = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
                if prior is not None:
                    authority = json.loads(prior.declared_authority_json)
                    if authority.get("payload_digest") != payload_digest: raise MoltbookError("moltbook_idempotency_conflict")
                    from src.workflows.job_runtime import _serialize
                    return _serialize(prior)
                row.active_job_id, row.active_deadline_at, row.active_payload_digest = job_id, deadline, payload_digest
                db.add(row)
            authority = {"principal": owner.principal_id, "owner_kind": "user", "session_id": owner.session_id,
                "capability_id": CAPABILITY, "connection_id": row.id, "connection_revision": row.revision,
                "vault_binding_digest": row.credential_binding, "consent_digest": digest(consent),
                "payload_digest": payload_digest, "operation": operation,
                "account_id": row.account_id, "account_name": row.account_name,
                "permissions": ["moltbook_read", "credential_egress", "workspace_write"] + (["external_mutation"] if operation in WRITES else []),
                "no_learning": True}
            if operation in WRITES:
                authority["review_digest"] = digest(review)
        # Never regenerate an already persisted ciphertext on exact replay.
        reference = PREFIX + digest(job_id.encode()) + ".input.enc"
        path = canonical_workspace_root(settings.workspace_dir) / reference
        if path.exists():
            encrypted_payload, truncated = _read_workspace_text_bounded(_safe_resolve(reference), max_bytes=32768)
            if truncated or digest(json.loads(decrypt(encrypted_payload))) != payload_digest:
                raise MoltbookError("moltbook_private_input_changed")
        else:
            _write_payload(path, encrypt(canonical(payload).decode()).encode())
        return await self.jobs.admit_job(DurableJobSpec(identity=DurableJobIdentity(job_id=job_id,
            owner_kind="user", owner_principal_id=owner.principal_id, job_kind=JOB_KIND,
            capability_version="1", idempotency_scope="moltbook-operation", idempotency_key=request_key),
            inputs={"payload_ref": reference, "payload_digest": payload_digest, "no_learning": True},
            session_id=owner.session_id, operator_session_id=owner.session_id, goal_id=goal_id,
            goal_revision=goal_revision, priority=priority, declared_authority=authority,
            deadline_at=deadline, max_attempts=1, budget_microusd=0,
            run_fingerprint=digest([payload_digest, authority, priority])))

    async def snapshot(self, owner, job_id):
        async with engine.get_session() as db:
            await root_current(db, owner.principal_id, owner.session_id)
            run = await self.jobs._fetch(db, job_id)
            if (run.owner_principal_id != owner.principal_id or run.operator_session_id != owner.session_id
                or run.job_kind != JOB_KIND or run.capability_version != "1"):
                raise MoltbookError("moltbook_job_owner_mismatch", status_code=404)
            from src.workflows.job_runtime import _serialize
            result = _serialize(run)
            value = {}
            if run.checkpoint_receipts_json not in (None, "", "[]"):
                value = state(run)
                history = value.get("approvals", [])
                if not isinstance(history, list) or len(history) > 2:
                    raise MoltbookError("moltbook_original_approval_history_invalid")
                result["approvals"] = []
                for entry in history:
                    prior = await db.get(ApprovalRequest, entry["id"])
                    if (prior and prior.owner_principal_id == owner.principal_id and prior.operator_session_id == owner.session_id
                        and prior.fingerprint == entry["fingerprint"]
                        and digest(json.loads(prior.details_json)["approval_scope"]) == entry["scope_digest"]):
                        result["approvals"].append({"id": prior.id, "status": prior.status, "scope_digest": entry["scope_digest"]})
                approval = await db.get(ApprovalRequest, value.get("approval_id")) if value.get("approval_id") else None
                if (approval and approval.owner_principal_id == owner.principal_id
                    and approval.operator_session_id == owner.session_id
                    and approval.fingerprint == value.get("approval_fingerprint")):
                    result["approval"] = {"id": approval.id, "status": approval.status,
                        "scope_digest": value.get("approval_scope_digest"), "expires_at": approval.expires_at}
            authority = json.loads(run.declared_authority_json)
            connection = await db.get(MoltbookConnection, authority["connection_id"])
            preview_key = connection.vault_key if connection and connection.credential_binding == authority["vault_binding_digest"] else None
        # Private input/Vault readback is staged after the SQL read session.
        # It can expose only the canonical original draft, never credentials or
        # verification codes. Historical metadata remains inspectable on drift.
        if authority["operation"] in READS | WRITES:
            try:
                if preview_key is None: raise MoltbookError("moltbook_original_credential_changed")
                credential = await vault_repository.snapshot(preview_key, owner_principal_id=owner.principal_id)
                raw, truncated = _read_workspace_text_bounded(_safe_resolve(PREFIX+digest(job_id.encode())+".input.enc"), max_bytes=32768)
                payload = json.loads(decrypt(raw))
                if (truncated or digest(payload) != authority["payload_digest"] or credential is None
                    or credential.binding_digest != authority["vault_binding_digest"] or credential.value in canonical(payload).decode()):
                    raise MoltbookError("moltbook_original_draft_unavailable")
                result["admission_request"] = {"operation": payload["operation"], "fields": payload["fields"],
                    "request_key": result["idempotency"]["key"], "goal_id": result["goal_id"],
                    "goal_revision": result["goal_revision"], "expected_revision": authority["connection_revision"]}
                if authority["operation"] in WRITES:
                    result["draft"] = {"operation": payload["operation"], "fields": payload["fields"],
                        "review": payload["review"], "payload_digest": authority["payload_digest"]}
                    result["admission_request"].update(community_job_id=payload["review"]["job_id"],
                        community_digest=payload["review"]["artifact_digest"], introductions_allowed=True, public_only=True)
                if value.get("answer_vault_key"):
                    answer = await vault_repository.snapshot(value["answer_vault_key"], owner_principal_id=owner.principal_id)
                    if answer and answer.binding_digest == value.get("answer_binding"):
                        result["manual_answer"] = text(answer.value, 32)
            except Exception:
                result["draft_unavailable"] = "moltbook_original_private_draft_binding_unavailable"
        result.update(no_learning=True, remote_data="untrusted_literal_owner_personal_noncommercial_no_redistribution")
        return result

    async def prepare_write(self, owner, **request):
        from src.integrations.moltbook_mutations import prepare_write
        return await prepare_write(self, owner, **request)

    async def execute(self, owner, job_id, *, request_key, expected_phase, fencing_token):
        identifier(request_key)
        projected = await self.snapshot(owner, job_id)
        receipts = next((c.get("payload", {}) for c in projected["checkpoints"] if c.get("checkpoint_id") == STATE_ID), {})
        for receipt in receipts.get("executions", []):
            if receipt.get("request_key") == request_key:
                if receipt != {"request_key": request_key, "phase": expected_phase, "fencing_token": fencing_token}:
                    raise MoltbookError("moltbook_execution_request_idempotency_conflict")
                # Reconciliation of this exact applied request can never run a
                # later phase, even when its separate approval now exists.
                return projected
        actual_phase = receipts.get("phase", "unattempted")
        if (projected["lease"]["fencing_token"] != fencing_token or actual_phase != expected_phase
            or any(r.get("phase") == expected_phase for r in receipts.get("executions", []))):
            raise MoltbookError("moltbook_original_execution_phase_or_fence_changed")
        execution = {"request_key": request_key, "phase": expected_phase, "fencing_token": fencing_token}
        if projected["declared_authority"].get("operation") in WRITES:
            from src.integrations.moltbook_mutations import execute_write
            return await execute_write(self, owner, job_id, execution=execution)
        return await self.execute_read(owner, job_id, execution=execution)

    async def approve(self, owner, job_id, **request):
        from src.integrations.moltbook_mutations import approve
        return await approve(self, owner, job_id, **request)

    async def manual_answer(self, owner, job_id, **request):
        from src.integrations.moltbook_mutations import manual_answer
        return await manual_answer(self, owner, job_id, **request)

    async def cancel(self, owner, job_id, **request):
        from src.integrations.moltbook_recovery import cancel
        return await cancel(self, owner, job_id, **request)

    async def recover(self, owner, job_id):
        from src.integrations.moltbook_recovery import recover
        return await recover(self, owner, job_id)

    async def execute_read(self, owner, job_id, *, execution):
        projected = await self.snapshot(owner, job_id)
        if projected["lease"]["fencing_token"] != execution["fencing_token"]:
            raise MoltbookError("moltbook_original_execution_fence_changed")
        if projected["status"] == "accepted":
            projected = await self.jobs.queue_job(job_id, expected_revision=projected["revision"])
        if projected["status"] != "queued": raise MoltbookError("moltbook_explicit_recovery_required")
        async with engine.get_session() as db:
            run = await self.jobs._fetch(db, job_id)
            row = await self.current(db, owner, run)
            authority = json.loads(run.declared_authority_json)
            # Durable arguments retain a redacted shape, not executable input.
            # Derive the one fixed physical reservation and independently
            # bind its digest to the canonical admitted input above.
            reference = PREFIX + digest(job_id.encode()) + ".input.enc"
            ciphertext, truncated = _read_workspace_text_bounded(_safe_resolve(reference), max_bytes=32768)
            payload = json.loads(decrypt(ciphertext))
            if truncated or digest(payload) != authority["payload_digest"]: raise MoltbookError("moltbook_private_input_changed")
            credential = await vault_repository.snapshot(row.vault_key, owner_principal_id=owner.principal_id)
            if credential is None or credential.binding_digest != authority["vault_binding_digest"]:
                raise MoltbookError("moltbook_credential_changed")
            deadline = utc(run.deadline_at)
        runner = "moltbook:" + uuid.uuid4().hex
        projected = await self.jobs.claim_job(job_id, owner=runner, expected_revision=projected["revision"],
            expected_fencing_token=projected["lease"]["fencing_token"], lease_seconds=30)
        lease = (runner, projected["lease"]["fencing_token"])
        async with engine.get_session() as db:
            await writer(db)
            run = await self.jobs._fetch(db, job_id)
            await self.current(db, owner, run, lease=lease)
            if run.checkpoint_receipts_json not in (None, "[]", ""):
                raise MoltbookError("moltbook_original_contact_not_replayable")
            save_state(run, {"phase": "prepared", "calls": [], "executions": [execution],
                "max_contacts": 2 if payload["operation"] == "inspect" else 1})
            db.add(run)
        task = asyncio.current_task()
        adapter = MoltbookAdapter(transport=self.adapter.transport, resolver=self.adapter.resolver)
        self._active[job_id] = task
        try:
            results = []
            operations = [("me", {}), ("status", {})] if payload["operation"] == "inspect" else [(payload["operation"], payload["fields"])]
            for operation, fields in operations:
                async def contact():
                    async with engine.get_session() as db:
                        await writer(db)
                        run = await self.jobs._fetch(db, job_id)
                        await self.current(db, owner, run, lease=lease)
                        value = state(run)
                        if len(value["calls"]) >= value["max_contacts"] or any(c["status"] == "intent" for c in value["calls"]):
                            raise MoltbookError("moltbook_contact_budget_or_uncertainty")
                        value["calls"].append({"operation": operation, "request_digest": digest(fields), "status": "intent"})
                        value["phase"] = "contact_started"
                        save_state(run, value)
                        db.add(run)
                try:
                    response = await adapter.call(operation, fields, key=credential.value, deadline=deadline, before_contact=contact)
                except MoltbookError:
                    receipt = adapter.read_response_receipt
                    if receipt is not None and receipt["operation"] == operation and adapter.marker.snapshot()["status"] == "verified":
                        async with engine.get_session() as db:
                            await writer(db)
                            run = await self.jobs._fetch(db, job_id)
                            self.jobs._assert_lease(run, owner=lease[0], fencing_token=lease[1])
                            value = state(run)
                            last = value["calls"][-1]
                            if (last["operation"] != operation or last["status"] != "intent"
                                or last["request_digest"] != digest(fields)):
                                raise MoltbookError("moltbook_contact_receipt_changed")
                            last.update(status="received", outcome="failed_read", http_status=receipt["http_status"],
                                response_digest=receipt["response_digest"], method="GET")
                            await self.retain_cooldown(db, owner, run, value, receipt, lease)
                            save_state(run, value); db.add(run)
                    raise
                results.append(response)
                async with engine.get_session() as db:
                    await writer(db)
                    run = await self.jobs._fetch(db, job_id)
                    # Settled network audit survives authority drift; adoption
                    # below still requires current Root/Goal/credential.
                    self.jobs._assert_lease(run, owner=lease[0], fencing_token=lease[1])
                    value = state(run)
                    value["calls"][-1]["status"] = "received"
                    value["calls"][-1]["response_digest"] = digest(response)
                    save_state(run, value)
                    db.add(run)
            normalized = normalize_read(payload["operation"], results)
            def redact(value):
                if isinstance(value, str): return value.replace(credential.value, "[redacted credential]")
                if isinstance(value, list): return [redact(item) for item in value]
                if isinstance(value, dict): return {k: redact(v) for k,v in value.items()}
                return value
            output = canonical({"schema": "seraph.moltbook.read.v1", "job_id": job_id,
                "operation": payload["operation"], "data": redact(normalized), "no_learning": True,
                "trust": "external_untrusted_literal", "use": "owner_personal_noncommercial_no_redistribution"})
            if len(output) > 65536: raise MoltbookError("moltbook_output_bound")
            output_ref = PREFIX + digest(job_id.encode()) + ".output.json"
            readback_receipt = {"effect_id": "moltbook-read:"+digest(output),
                "receipt_kind": "readback", "effect_type": "moltbook_read", "target_path": output_ref,
                "status": "succeeded", "content_sha256": digest(output), "readback_id": "physical:"+digest(output),
                "verified_at": now().isoformat(), "details": {"no_learning": True}}
            async with engine.get_session() as db:
                await writer(db)
                run = await self.jobs._fetch(db, job_id)
                await self.current(db, owner, run, lease=lease)
                value = state(run)
                if any(c["status"] != "received" for c in value["calls"]): raise MoltbookError("moltbook_response_unsettled")
                value.update(phase="verified_output_ready", verified_output={"output_ref": output_ref,
                    "output_digest": digest(output), "payload_digest": authority["payload_digest"],
                    "artifact_type": "moltbook_private_read", "terminal_phase": "adopted",
                    "account_update": normalized if payload["operation"] == "inspect" else None,
                    "readback_receipt": readback_receipt}, cleanup=adapter.marker.snapshot())
                save_state(run, value); db.add(run)
            _write_payload(canonical_workspace_root(settings.workspace_dir) / output_ref, output)
            observed, truncated = _read_workspace_text_bounded(_safe_resolve(output_ref), max_bytes=65536)
            if truncated or observed.encode() != output: raise MoltbookError("moltbook_output_readback_failed")
            artifact = build_artifact_record(file_path=output_ref, artifact_type="moltbook_private_read",
                producer=JOB_KIND, run_id=job_id, session_id=owner.session_id, content=output)
            async with engine.get_session() as db:
                await writer(db)
                run = await self.jobs._fetch(db, job_id)
                row = await self.current(db, owner, run, lease=lease)
                value = state(run)
                if any(c["status"] != "received" for c in value["calls"]): raise MoltbookError("moltbook_response_unsettled")
                value["phase"] = "adopted"
                value["output_ref"], value["output_digest"] = output_ref, digest(output)
                save_state(run, value)
                run.artifact_receipts_json = canonical([artifact]).decode()
                run.effect_receipts_json = canonical([readback_receipt]).decode()
                run.status, run.result_digest, run.result_summary = "succeeded", digest(output), "Bounded private Moltbook read verified; no learning"
                run.finished_at, run.lease_owner, run.lease_expires_at = now(), None, None
                if payload["operation"] == "inspect":
                    row.account_id, row.account_name = normalized["account_id"], normalized["account_name"]
                    row.mode = "active" if normalized["claimed"] else "pending_claim"
                row.active_job_id, row.active_deadline_at, row.active_payload_digest = None, None, ""
                db.add(run); db.add(row)
            return await self.snapshot(owner, job_id)
        except BaseException as exc:
            async with engine.get_session() as db:
                await writer(db)
                run = await self.jobs._fetch(db, job_id)
                if run.status == "running" and run.lease_owner == lease[0] and run.fencing_token == lease[1]:
                    value = state(run)
                    try:
                        connection = await self.current(db, owner, run, lease=lease, settlement_only=True)
                        if value.get("cancel_request"): connection = None
                    except (MoltbookError, BoardError): connection = None
                    uncertain = any(c["status"] != "received" for c in value["calls"])
                    if value.get("phase") != "verified_output_ready":
                        value["phase"] = "unknown" if uncertain else "blocked"
                    value["cleanup"] = adapter.marker.snapshot()
                    if value["cleanup"]["status"] == "verified":
                        value["worker_completed"] = {"fencing_token": lease[1], "transport_closed": True}
                        if (not uncertain and value.get("phase") != "verified_output_ready" and connection is not None):
                            # This native kind executes fixed GET routes only.
                            # Exact authority was checked in this same writer;
                            # actual awaited transfers have all closed.
                            connection.active_job_id, connection.active_deadline_at, connection.active_payload_digest = None, None, ""
                            value["preflight_slot_released"] = {"job_id": job_id, "fencing_token": lease[1],
                                "payload_digest": authority["payload_digest"], "no_post": True, "transport_closed": True}
                            db.add(connection)
                    save_state(run, value)
                    run.status = "unknown_external_effect" if uncertain else "blocked"
                    run.failure_reason = getattr(exc, "code", "moltbook_transfer_or_authority_failed")
                    run.lease_owner = run.lease_expires_at = None
                    db.add(run)
            if isinstance(exc, asyncio.CancelledError): raise
            if isinstance(exc, MoltbookError): raise
            raise MoltbookError("moltbook_transfer_or_authority_failed") from None
        finally:
            if self._active.get(job_id) is task: self._active.pop(job_id, None)

    async def output(self, owner, job_id):
        projected = await self.snapshot(owner, job_id)
        if projected["status"] != "succeeded" or len(projected["artifacts"]) != 1:
            raise MoltbookError("moltbook_verified_output_unavailable")
        artifact = projected["artifacts"][0]
        reference = PREFIX + digest(job_id.encode()) + ".output.json"
        if artifact.get("file_path") != reference: raise MoltbookError("moltbook_output_reference_invalid")
        raw, truncated = _read_workspace_text_bounded(_safe_resolve(reference), max_bytes=65536)
        if truncated or digest(raw.encode()) != artifact["content_sha256"]:
            raise MoltbookError("moltbook_output_changed")
        return json.loads(raw)


def normalize_read(operation, values):
    value = values[0]
    if operation == "inspect":
        agent = value.get("agent")
        if not isinstance(agent, dict): raise MoltbookError("moltbook_account_schema_invalid")
        status = values[1].get("status")
        if status not in {"claimed", "pending_claim"}: raise MoltbookError("moltbook_claim_status_unproven")
        return {"account_id": identifier(agent.get("id")), "account_name": text(agent.get("name"), 128),
            "claimed": status == "claimed", "human_claim": "external_manual_only"}
    if operation == "post": return safe_content(value.get("post"))
    if operation == "comments": return {"comments": safe_comments(value), "next_cursor": text(value.get("next_cursor", ""), 512, required=False)}
    if operation == "feed":
        posts = value.get("posts")
        if not isinstance(posts, list) or len(posts) > 10: raise MoltbookError("moltbook_feed_bound")
        return {"posts": [safe_content(post) for post in posts], "next_cursor": text(value.get("next_cursor", ""), 512, required=False)}
    if operation == "community":
        community = value.get("submolt")
        if not isinstance(community, dict) or community.get("is_private") is not False:
            raise MoltbookError("moltbook_public_community_unproven")
        return {"id": identifier(community.get("id")), "name": text(community.get("name"), 30),
            "description": text(community.get("description", ""), 4096, required=False),
            "is_private": False, "rules": text(community.get("rules", ""), 4096, required=False)}
    raise MoltbookError("moltbook_read_operation_invalid")


moltbook_service = MoltbookService()
