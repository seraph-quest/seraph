"""Exact Calendar reschedule snapshots and pure canonical writer operations.

Provider, Vault and file work belongs to the caller's staging phase. These
operations use the existing WorkflowRunState and ApprovalRequest rows only;
they never open a session or perform an external read inside a writer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json

from sqlalchemy import select, update

from src.auth.ownership import _current_root
from src.db.models import (ApprovalRequest, Goal, GoogleServiceConnection,
    CalendarEventBinding, CalendarReadConsent, CalendarRescheduleConsent, OperatorIdentity, WorkBoardAttempt,
    WorkBoardEvidenceDependency, WorkBoardInputArtifact, Secret,
    WorkBoardTask, WorkflowRunState)
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.integrations.calendar_reschedule_contract import READ_SERVICE, SEND_SERVICE, SCOPES, digest, fail
from src.workflows.job_runtime import (durable_job_repository,
    _assert_canonical_goal_fence, _append_goal_fence_condition, _goal_owner_binding)
from src.vault.repository import secret_binding_digest
from src.security.trust_contract import AuthorityGrant

SEND_KIND = "calendar_reschedule_v1"
OBSERVATION_KIND = "calendar_reschedule_observation_v1"
IDENTITY_KIND = "calendar_reschedule_identity_v1"
VERSION = "calendar-exact-reschedule-v1"


def utc(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


def canonical(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode()) > 64 * 1024:
        fail("checkpoint_bound")
    return encoded


def state(run):
    try:
        value = json.loads(run.checkpoint_context_json or "{}")
    except (ValueError, TypeError):
        fail("checkpoint_invalid")
    if not isinstance(value, dict) or (value and value.get("schema_version") != 1):
        fail("checkpoint_invalid")
    canonical(value)
    return value


def arguments(run):
    try:
        # Generic native arguments deliberately retain a redacted shape.
        # The fixed body-free admitted binding lives on this SAME run's
        # canonical checkpoint, checked against the original admission digest.
        value = state(run).get("admitted_inputs")
    except (ValueError, TypeError):
        fail("input_invalid")
    if not isinstance(value, dict) or digest(value) != run.input_digest or digest(value) != run.run_fingerprint:
        fail("input_changed")
    return value


async def current_root(db, operator):
    if AuthorityGrant.CAPABILITY_EXECUTE not in operator.principal.grants:
        fail("capability_permission_required")
    root = await _current_root(db, operator)
    if root.replaced_by_id:
        fail("root_replaced")
    if root.operator_identity_id:
        identity = await db.get(OperatorIdentity, root.operator_identity_id)
        if identity is None or identity.revoked_at is not None:
            fail("identity_revoked")
    return root


def finite_goal_grant(goal):
    from src.goals.repository import deserialize_admission_budget
    grant=deserialize_admission_budget(goal)
    current=datetime.now(timezone.utc)
    if (grant is None or grant.reviewed_grant is not True
        or not utc(grant.period_started_at)<=current<utc(grant.period_expires_at)):
        fail("finite_goal_grant_required")
    return grant


def connection_snapshot(row):
    return {"connection_id": row.connection_id, "revision": row.revision,
        "service": row.service, "credential_fingerprint": row.credential_fingerprint,
        "vault_secret_key": row.vault_secret_key,
        "declared_scopes_digest": digest(json.loads(row.declared_scopes_json))}


async def connection(db, operator, expected):
    row = await db.get(GoogleServiceConnection, expected["connection_id"], populate_existing=True)
    if (row is None or row.owner_principal_id != operator.principal.principal_id
        or row.owner_session_id != operator.session_id or row.state != "active"
        or row.service not in SCOPES or connection_snapshot(row) != {
            key: item for key, item in expected.items() if key != "vault_record_digest"}):
        fail("connection_changed")
    if frozenset(json.loads(row.declared_scopes_json)) != SCOPES[row.service]:
        fail("scope_not_exact")
    secret = (await db.execute(select(Secret).where(Secret.key == row.vault_secret_key,
        Secret.owner_principal_id == operator.principal.principal_id,
        Secret.revoked_at.is_(None)).execution_options(populate_existing=True))).scalar_one_or_none()
    if (secret is None or "vault_record_digest" not in expected
        or secret_binding_digest(secret) != expected["vault_record_digest"]):
        fail("credential_changed")
    return row


def consent_snapshot(row):
    return {"consent_id":row.consent_id,"revision":row.revision,"state":row.state,
        "goal_id":row.goal_id,"goal_revision":row.goal_revision,
        "profile_binding_digest":row.profile_binding_digest,"account_identity_digest":row.account_identity_digest,
        "selected_calendar_digest":row.selected_calendar_digest,
        "selected_calendar_ciphertext_digest":digest(row.selected_calendar_id_private.encode()),
        "event_binding_id":row.event_binding_id,"event_binding_revision":row.event_binding_revision,
        "event_identity_digest":row.event_identity_digest,"consent_digest":row.consent_digest,
        "expires_at":utc(row.expires_at).isoformat()}


async def selection_snapshot(db, operator, binding_id, *, expected_revision=None):
    binding = await db.get(CalendarEventBinding,binding_id,populate_existing=True)
    if (binding is None or binding.state!="selected" or binding.owner_principal_id!=operator.principal.principal_id
        or binding.owner_session_id!=operator.session_id or (expected_revision is not None and binding.revision!=expected_revision)):
        fail("selected_event_changed")
    grant = await db.get(CalendarReadConsent,binding.consent_id,populate_existing=True)
    profile = await db.get(GoogleServiceConnection,binding.connection_id,populate_existing=True)
    if (grant is None or profile is None or grant.state!="active" or utc(grant.expires_at)<=datetime.now(timezone.utc)
        or grant.owner_principal_id!=operator.principal.principal_id or grant.owner_session_id!=operator.session_id
        or profile.owner_principal_id!=operator.principal.principal_id or profile.owner_session_id!=operator.session_id
        or profile.state!="active" or profile.service!="calendar_readonly"
        or grant.revision!=binding.consent_revision or profile.revision!=binding.connection_revision
        or grant.connection_id!=profile.connection_id or grant.connection_revision!=profile.revision):
        fail("selection_read_permission_changed")
    await _assert_canonical_goal_fence(db,goal_id=grant.goal_id,goal_revision=grant.goal_revision,
        owner_kind="user",owner_principal_id=grant.owner_principal_id,session_id=grant.owner_session_id)
    return {"event_binding_id":binding.event_binding_id,"event_binding_revision":binding.revision,
        "goal_id":grant.goal_id,"goal_revision":grant.goal_revision,
        "calendar_ciphertext_digest":digest(binding.calendar_id_private.encode()),
        "event_ciphertext_digest":digest(binding.provider_event_id_private.encode()),
        "provider_identity_digest":binding.provider_identity_digest,
        "event_revision":binding.event_revision,"calendar_list_revision":binding.calendar_list_revision,
        "selection_consent_id":grant.consent_id,"selection_consent_revision":grant.revision,
        "selection_connection_id":profile.connection_id,"selection_connection_revision":profile.revision}


async def consent(db, operator, expected, *, connections=None):
    row = await db.get(CalendarRescheduleConsent,expected["consent_id"],populate_existing=True)
    if (row is None or row.owner_principal_id!=operator.principal.principal_id
        or row.original_root_session_id!=operator.session_id or row.state!="active"
        or utc(row.expires_at)<=datetime.now(timezone.utc)
        or any(flag is not True for flag in (row.owned_event_read_allowed,row.calendar_list_metadata_read_allowed,row.one_conditional_reschedule_allowed))
        or consent_snapshot(row)!=expected):
        fail("write_consent_changed")
    if connections is not None and (len(connections)!=2 or row.profile_binding_digest!=digest(connections)
        or (row.read_connection_id,row.read_connection_revision)!=(connections[0]["connection_id"],connections[0]["revision"])
        or (row.write_connection_id,row.write_connection_revision)!=(connections[1]["connection_id"],connections[1]["revision"])):
        fail("write_consent_profile_changed")
    goal=await _assert_canonical_goal_fence(db,goal_id=row.goal_id,goal_revision=row.goal_revision,
        owner_kind="user",owner_principal_id=row.owner_principal_id,session_id=row.original_root_session_id)
    finite_goal_grant(goal)
    return row


async def source_snapshot(db, operator, task_id, *, binding_hint):
    """Metadata only; typed bytes/provider IDs are staged after writer exit."""
    task=(await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id==task_id)
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if (task is None or task.owner_principal_id!=operator.principal.principal_id
        or task.owner_session_id!=operator.session_id or task.capability_id!="calendar.event.reschedule.v1"
        or str(getattr(task.status,"value",task.status)) in {"cancelled","archived"}
        or task.archived_at is not None):
        fail("task_unavailable")
    artifact=await db.get(WorkBoardInputArtifact,task.input_artifact_id,populate_existing=True)
    if (artifact is None or artifact.owner_principal_id!=task.owner_principal_id
        or artifact.owner_session_id!=task.owner_session_id or artifact.bound_task_id!=task.task_id
        or artifact.state not in {"bound","consumed"} or utc(artifact.expires_at)<=datetime.now(timezone.utc)
        or artifact.payload_sha256!=task.typed_input_digest):
        fail("task_input_changed")
    grant=await consent(db,operator,binding_hint["consent"])
    binding=await db.get(CalendarEventBinding,grant.event_binding_id,populate_existing=True)
    if (binding is None or binding.state!="selected" or binding.owner_principal_id!=task.owner_principal_id
        or binding.owner_session_id!=task.owner_session_id or binding.revision!=grant.event_binding_revision
        or binding.provider_identity_digest!=grant.event_identity_digest
        or task.goal_id!=grant.goal_id or task.goal_revision!=grant.goal_revision):
        fail("selected_event_changed")
    read_consent=await db.get(CalendarReadConsent,binding.consent_id,populate_existing=True)
    source_connection=await db.get(GoogleServiceConnection,binding.connection_id,populate_existing=True)
    if (read_consent is None or source_connection is None or read_consent.state!="active"
        or utc(read_consent.expires_at)<=datetime.now(timezone.utc)
        or read_consent.owner_principal_id!=task.owner_principal_id or read_consent.owner_session_id!=task.owner_session_id
        or read_consent.revision!=binding.consent_revision or read_consent.connection_id!=binding.connection_id
        or read_consent.connection_revision!=binding.connection_revision or source_connection.state!="active"
        or source_connection.owner_principal_id!=task.owner_principal_id or source_connection.owner_session_id!=task.owner_session_id
        or source_connection.revision!=binding.connection_revision
        or read_consent.goal_id!=task.goal_id or read_consent.goal_revision!=task.goal_revision):
        fail("selection_read_permission_changed")
    if await db.scalar(select(WorkBoardEvidenceDependency.dependency_id).where(
        WorkBoardEvidenceDependency.task_id==task_id).limit(1)) is not None:
        fail("task_dependency_unsupported")
    from src.work_board.input_artifacts import _metadata_digest
    if artifact.metadata_digest!=_metadata_digest(artifact): fail("task_artifact_metadata_changed")
    return {"task_id":task_id,"task_revision":task.task_revision,"input_artifact_id":artifact.artifact_id,
        "input_digest":task.typed_input_digest,"input_artifact_metadata_digest":artifact.metadata_digest,
        "goal_id":task.goal_id,"goal_revision":task.goal_revision,
        "event_binding_id":binding.event_binding_id,"event_binding_revision":binding.revision,
        "event_revision":binding.event_revision,"calendar_list_revision":binding.calendar_list_revision,
        "provider_identity_digest":binding.provider_identity_digest,
        "calendar_ciphertext_digest":digest(binding.calendar_id_private.encode()),
        "event_ciphertext_digest":digest(binding.provider_event_id_private.encode()),
        "selection_consent_id":read_consent.consent_id,"selection_consent_revision":read_consent.revision,
        "selection_connection_id":source_connection.connection_id,"selection_connection_revision":source_connection.revision,
        "consent":consent_snapshot(grant)}


@dataclass(frozen=True)
class ReplyLease:
    job_id: str
    owner: str
    fencing_token: int


async def authority(db, operator, run, *, lease=None, source_required=True):
    """Pure SQL current authority, including the immutable admitted snapshot."""
    await current_root(db, operator)
    if (run.job_kind not in {SEND_KIND, OBSERVATION_KIND, IDENTITY_KIND} or run.capability_version != VERSION
        or run.owner_kind != "user" or run.owner_principal_id != operator.principal.principal_id
        or run.operator_session_id != operator.session_id or run.session_id != operator.session_id):
        fail("owner_changed")
    inputs = arguments(run)
    goal=await _assert_canonical_goal_fence(db, goal_id=run.goal_id, goal_revision=run.goal_revision,
        owner_kind=run.owner_kind, owner_principal_id=run.owner_principal_id,
        session_id=run.session_id, authority=run.declared_authority_json)
    finite_goal_grant(goal)
    if json.loads(run.declared_authority_json).get("goal_grant_digest")!=digest(goal.admission_budget_json):
        fail("goal_grant_changed")
    if utc(run.deadline_at) <= datetime.now(timezone.utc):
        fail("deadline_expired")
    if lease is not None:
        if lease.job_id != run.run_identity or run.status != "running":
            fail("execution_changed")
        durable_job_repository._assert_lease(run, owner=lease.owner, fencing_token=lease.fencing_token)
    for expected in inputs["connections"]:
        await connection(db, operator, expected)
    if "selection" in inputs and await selection_snapshot(db,operator,inputs["selection"]["event_binding_id"])!=inputs["selection"]:
        fail("selected_event_changed")
    if run.job_kind == OBSERVATION_KIND:
        original = await durable_job_repository._fetch(db, inputs["original_job_id"])
        await assert_original_recovery(db, operator, original, expected_revision=inputs["original_revision"])
        if digest(state(original).get("intent")) != inputs["original_intent_digest"]:
            fail("original_binding_changed")
    if source_required:
        await consent(db, operator, inputs["source"]["consent"], connections=inputs["connections"])
        expected = inputs["source"]
        if await source_snapshot(db, operator, expected["task_id"], binding_hint=expected) != expected:
            fail("draft_source_changed")
    return inputs


async def cas(db, run, values, *, current_goal=True):
    conditions = [WorkflowRunState.id == run.id, WorkflowRunState.revision == run.revision,
        WorkflowRunState.status == run.status, WorkflowRunState.fencing_token == run.fencing_token]
    if current_goal:
        _append_goal_fence_condition(conditions, run)
    now = datetime.now(timezone.utc)
    changed = await db.execute(update(WorkflowRunState).where(*conditions)
        .values(**values, revision=WorkflowRunState.revision + 1, updated_at=now)
        .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        fail("writer_conflict")


async def claim_intent(db, operator, run, *, lease, intent, approval_id):
    """Consume exact approved authority and install the sole send effect atomically."""
    await authority(db, operator, run, lease=lease)
    checkpoint = state(run)
    if checkpoint.get("intent"):
        if checkpoint["intent"] != intent:
            fail("intent_conflict")
        return False
    preview = checkpoint.get("preview")
    if (not isinstance(preview, dict) or preview.get("approval_id") != approval_id
        or preview.get("source_etag_digest") != intent.get("source_etag_digest")
        or preview.get("request_digest") != intent.get("request_digest")
        or datetime.now(timezone.utc).timestamp() >= preview.get("expires_at", 0)
        or intent.get("source_digest") != preview.get("source_digest")
        or not 0 <= datetime.now(timezone.utc).timestamp() - intent.get("source_observed_at", 0) <= 10):
        fail("preview_changed")
    inputs = arguments(run)
    required = {"schema_version": 1, "job_id": run.run_identity,
        "effect_id": run.run_identity+":patch-once", "input_digest": run.input_digest,
        "authority_digest": run.authority_digest, "budget_digest": run.budget_digest,
        "original_root": run.operator_session_id, "owner_principal_id": run.owner_principal_id,
        "goal_id": run.goal_id, "goal_revision": run.goal_revision,
        "attempt": run.attempt_count, "fencing_token": lease.fencing_token,
        "approval_id": approval_id, "source_etag_digest": preview["source_etag_digest"],
        "new_start_epoch":preview["new_start_epoch"],
        "request_digest": preview["request_digest"], "source_digest": preview["source_digest"],
        "account_digest": inputs["account_digest"],
        "preview_artifact": checkpoint["private_artifacts"]["preview"]}
    if set(intent) != set(required) | {"source_observed_at"} or any(intent[key] != item for key, item in required.items()):
        fail("intent_binding_changed")
    row = await db.get(ApprovalRequest, approval_id, populate_existing=True)
    # Empty MIME does not prove an empty approval attachment handoff. Enforce
    # the actual row before the existing consumer can validate attachments.
    if row is None or json.loads(row.attachment_refs_json or "[]") != []:
        fail("approval_attachments_forbidden")
    if row.tool_name != "calendar.event.reschedule.v1" or row.fingerprint != preview.get("approval_fingerprint"):
        fail("approval_changed")
    details = json.loads(row.details_json or "{}")
    scope = details.get("approval_scope")
    if (not isinstance(scope, dict) or digest(scope) != preview.get("scope_digest")
        or details.get("scope_digest") != digest(scope)
        or row.fingerprint != fingerprint_tool_call("calendar.event.reschedule.v1", {"scope_digest": digest(scope)})):
        fail("approval_scope_changed")
    approved = await approval_repository._consume_approved_for_resume_in_session(db,
        quarantine_in_separate_session=False, approval_id=approval_id,
        owner_operator_session_id=operator.session_id, operator_principal_id=operator.principal.principal_id,
        job_id=run.run_identity, owner_kind=run.owner_kind, owner_principal_id=run.owner_principal_id,
        service_id=None, approval_owner_principal_id=run.owner_principal_id,
        authority_digest=run.authority_digest, goal_id=run.goal_id, goal_revision=run.goal_revision,
        plan_revision=run.plan_revision, capability_version=run.capability_version,
        budget_digest=run.budget_digest, expires_at=preview["expires_at"],
        session_id=run.session_id, conversation_id=run.conversation_id, criterion_id=None, candidate_id=None)
    if approved is None:
        fail("approval_unavailable")
    effects = json.loads(run.effect_receipts_json)
    if effects:
        fail("effect_conflict")
    effect = {"effect_id": intent["effect_id"], "receipt_kind": "write",
        "effect_type": "calendar_reschedule", "status": "claimed",
        "target_digest": intent["request_digest"], "details": {"reconciliation_required": True}}
    checkpoint["intent"] = intent
    await cas(db, run, {"checkpoint_context_json": canonical(checkpoint),
        "effect_receipts_json": canonical([effect])})
    return True


async def mark_dispatch(db, operator, run, *, lease, intent_digest):
    await authority(db, operator, run, lease=lease)
    checkpoint = state(run)
    intent = checkpoint.get("intent")
    if not isinstance(intent, dict) or digest(intent) != intent_digest or checkpoint.get("contact_may_have_occurred"):
        fail("dispatch_slot_consumed")
    if not 0 <= datetime.now(timezone.utc).timestamp() - intent.get("source_observed_at", 0) <= 10:
        fail("source_observation_expired")
    if datetime.now(timezone.utc).timestamp()>=intent.get("new_start_epoch",0):
        fail("proposed_start_expired")
    effects = json.loads(run.effect_receipts_json)
    if len(effects) != 1 or effects[0].get("effect_id") != intent["effect_id"] or effects[0].get("status") != "claimed":
        fail("effect_changed")
    checkpoint["contact_may_have_occurred"] = True
    checkpoint["dispatch_fence"] = run.fencing_token
    checkpoint["transport_quiescent"] = False
    checkpoint.pop("transport_closure", None)
    effects[0]["status"] = "unknown"
    await cas(db, run, {"checkpoint_context_json": canonical(checkpoint), "effect_receipts_json": canonical(effects)})


async def assert_original_recovery(db, operator, original, *, expected_revision=None):
    """Pure original-row proof shared by precontact and adoption writers."""
    checkpoint = state(original)
    intent = checkpoint.get("intent")
    if (original.job_kind != SEND_KIND or original.capability_version != VERSION
        or original.owner_principal_id != operator.principal.principal_id
        or original.operator_session_id != operator.session_id
        or (expected_revision is not None and original.revision != expected_revision)
        or not isinstance(intent, dict) or original.status != "unknown_external_effect"
        or original.lease_owner is not None or original.lease_expires_at is not None
        or checkpoint.get("transport_quiescent") is not True):
        fail("original_recovery_unavailable")
    original_bindings = {"job_id": original.run_identity, "input_digest": original.input_digest,
        "authority_digest": original.authority_digest, "budget_digest": original.budget_digest,
        "original_root": original.operator_session_id, "owner_principal_id": original.owner_principal_id,
        "goal_id": original.goal_id, "goal_revision": original.goal_revision,
        "attempt": original.attempt_count, "fencing_token": original.fencing_token,
        "effect_id": original.run_identity+":patch-once"}
    if any(intent.get(key) != item for key, item in original_bindings.items()):
        fail("original_binding_changed")
    records = checkpoint.get("contacts")
    closure = checkpoint.get("transport_closure")
    if (not isinstance(records, list) or not 1 <= len(records) <= 13
        or not isinstance(closure, dict)
        or set(closure) != {"schema_version", "kind", "job_id", "original_root", "owner_principal_id",
            "attempt", "fencing_token", "lease_owner", "intent_digest", "contact_history_digest",
            "last_slot", "closed_revision", "closed_at", "adapters"}
        or closure.get("schema_version") != 1 or closure.get("kind") != "calendar_reschedule_owned_transport_closure_v1"
        or any(closure.get(key) != original_bindings[key] for key in
            ("job_id", "original_root", "owner_principal_id", "attempt", "fencing_token"))
        or closure.get("intent_digest") != digest(intent)
        or closure.get("contact_history_digest") != digest(records)
        or closure.get("last_slot") != len(records)
        or not isinstance(closure.get("lease_owner"), str) or not closure["lease_owner"]
        or type(closure.get("closed_revision")) is not int or not 1 <= closure["closed_revision"] <= original.revision):
        fail("original_transport_closure_unavailable")
    try:
        closed_at = datetime.fromisoformat(closure["closed_at"])
        if closed_at.tzinfo is None or closed_at.utcoffset() is None:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        fail("original_transport_closure_unavailable")
    phase = []
    for slot, record in enumerate(records, 1):
        if (not isinstance(record, dict) or record.get("slot") != slot
            or record.get("service") not in {READ_SERVICE, SEND_SERVICE}
            or record.get("operation") not in {"refresh", "identity", "calendar", "event", "patch"}
            or type(record.get("fence")) is not int or not 1 <= record["fence"] <= original.fencing_token
            or record.get("status") not in {"received", "reserved"}):
            fail("original_contact_history_changed")
        if record["fence"] == original.fencing_token:
            phase.append(record)
        elif record["status"] != "received":
            fail("original_contact_history_changed")
        if record["status"] == "reserved" and (slot != len(records) or record["fence"] != original.fencing_token):
            fail("original_contact_history_changed")
    adapters = closure.get("adapters")
    if not isinstance(adapters, list) or not 1 <= len(adapters) <= 2:
        fail("original_transport_closure_unavailable")
    seen = set()
    started = 0
    for adapter in adapters:
        if not isinstance(adapter, dict) or set(adapter) != {"service", "transport"}:
            fail("original_transport_closure_unavailable")
        service = adapter["service"]
        proof = adapter["transport"]
        if (service not in {READ_SERVICE, SEND_SERVICE} or service in seen or not isinstance(proof, dict)
            or set(proof) != {"status", "active_operations", "unsettled_operations", "requests_started", "requests_settled"}
            or proof.get("status") != "verified"
            or any(type(proof.get(key)) is not int for key in
                ("active_operations", "unsettled_operations", "requests_started", "requests_settled"))
            or proof["active_operations"] != 0 or proof["unsettled_operations"] != 0
            or not 0 <= proof["requests_started"] <= 9 or proof["requests_started"] != proof["requests_settled"]
            or proof["requests_started"] < sum(record["service"] == service for record in phase)):
            fail("original_transport_closure_unavailable")
        seen.add(service)
        started += proof["requests_started"]
    # One failing DNS/authority check may close before a contact reservation.
    # It cannot authorize a missing reserved contact or a second provider call.
    if not phase or not len(phase) <= started <= len(phase) + 1 or any(record["service"] not in seen for record in phase):
        fail("original_transport_closure_unavailable")
    sends = [record for record in records if record["operation"] == "patch"]
    if (len(sends) > 1 or any(record["service"] != SEND_SERVICE or record["fence"] != original.fencing_token for record in sends)
        or (checkpoint.get("contact_may_have_occurred") is True
            and (len(sends) != 1 or checkpoint.get("dispatch_fence") != original.fencing_token))
        or (checkpoint.get("contact_may_have_occurred") is not True and sends)):
        fail("original_dispatch_changed")
    preview = checkpoint.get("preview", {})
    approval = await db.get(ApprovalRequest, intent.get("approval_id"), populate_existing=True)
    if (approval is None or approval.status != "consumed"
        or approval.id != preview.get("approval_id")
        or approval.owner_principal_id != original.owner_principal_id
        or approval.operator_session_id != original.operator_session_id
        or approval.fingerprint != preview.get("approval_fingerprint")):
        fail("original_approval_changed")
    old_goal = await db.get(Goal, original.goal_id, populate_existing=True)
    if old_goal is None or _goal_owner_binding(old_goal) != (original.owner_principal_id, original.session_id):
        fail("original_provenance_changed")
    effects = json.loads(original.effect_receipts_json)
    if len(effects) != 1 or effects[0].get("effect_id") != intent["effect_id"] or effects[0].get("status") != "unknown":
        fail("original_liability_changed")
    return checkpoint, intent, effects


async def append_observation(db, operator, original, auxiliary, *, lease,
    original_revision, original_intent_digest, observation):
    """Observation-only recovery and auxiliary completion in the SAME writer."""
    await authority(db, operator, auxiliary, lease=lease, source_required=False)
    if auxiliary.job_kind != OBSERVATION_KIND:
        fail("recovery_kind_invalid")
    checkpoint, intent, effects = await assert_original_recovery(db, operator, original, expected_revision=original_revision)
    inputs = arguments(auxiliary)
    if (digest(intent) != original_intent_digest or inputs.get("original_job_id") != original.run_identity
        or inputs.get("original_intent_digest") != original_intent_digest):
        fail("recovery_input_changed")
    history = effects[0].get("observation_history", [])
    if not isinstance(history, list) or len(history) >= 16 or any(item.get("auxiliary_job_id") == auxiliary.run_identity for item in history):
        fail("recovery_history_conflict")
    if set(observation) != {"outcome", "response_digest", "private_artifact", "no_learning"}:
        fail("observation_invalid")
    record = {**observation, "schema_version": 1, "auxiliary_job_id": auxiliary.run_identity,
        "effect_id": intent["effect_id"], "original_intent_digest": original_intent_digest,
        "recovery_goal_id": auxiliary.goal_id, "recovery_goal_revision": auxiliary.goal_revision,
        "observed_at": datetime.now(timezone.utc).isoformat()}
    if observation.get("outcome") not in {"verified_reschedule_observation", "unknown_observation"} or observation.get("no_learning") is not True:
        fail("observation_invalid")
    effects[0]["observation_history"] = [*history, record]
    # Original immutable checkpoint, status, effect status, authority, fence,
    # lease, deadline and Goal are deliberately absent from this update.
    await cas(db, original, {"effect_receipts_json": canonical(effects)}, current_goal=False)
    await durable_job_repository.complete_calendar_observation_in_session(db, auxiliary,
        owner=lease.owner, fencing_token=lease.fencing_token, observation=record)
