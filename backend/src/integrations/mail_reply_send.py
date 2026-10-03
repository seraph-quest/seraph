"""Exact Mail reply snapshots and pure canonical writer operations.

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
    MailMessageBinding, MailReadConsent, OperatorIdentity, WorkBoardAttempt,
    WorkBoardEvidenceDependency, WorkBoardInputArtifact, Secret,
    WorkBoardTask, WorkflowRunState)
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.integrations.gmail_send import READ_SERVICE, SEND_SERVICE, SCOPES, digest, fail
from src.workflows.job_runtime import (durable_job_repository,
    _assert_canonical_goal_fence, _append_goal_fence_condition, _goal_owner_binding)
from src.vault.repository import secret_binding_digest
from src.security.trust_contract import AuthorityGrant

SEND_KIND = "mail_reply_send_v1"
OBSERVATION_KIND = "mail_reply_observation_v1"
IDENTITY_KIND = "mail_reply_identity_v1"
VERSION = "mail-exact-reply-v1"


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


async def source_snapshot(db, operator, task_id, *, binding_hint):
    """Stage metadata only. The encrypted artifact is read after session exit."""
    task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id)
        .execution_options(populate_existing=True))).scalar_one_or_none()
    if (task is None or task.owner_principal_id != operator.principal.principal_id
        or task.owner_session_id != operator.session_id
        or task.capability_id != "work.mail-reply-draft.v1"
        or str(getattr(task.status, "value", task.status)) not in {"done", "review"}):
        fail("draft_unavailable")
    attempt = (await db.execute(select(WorkBoardAttempt).where(
        WorkBoardAttempt.task_id == task_id).order_by(WorkBoardAttempt.created_at.desc()))).scalars().first()
    if attempt is None or not attempt.workflow_run_id:
        fail("draft_unavailable")
    run = await durable_job_repository._fetch(db, attempt.workflow_run_id)
    if (run.status != "succeeded" or run.job_kind != "mail_reply_draft"
        or run.owner_principal_id != task.owner_principal_id
        or run.operator_session_id != task.owner_session_id
        or run.goal_id != task.goal_id or run.goal_revision != task.goal_revision):
        fail("draft_unavailable")
    artifacts = json.loads(run.artifact_receipts_json)
    effects = json.loads(run.effect_receipts_json)
    matches = [item for item in artifacts if item.get("artifact_type") == "mail_reply_draft"
        and item.get("exists") is True and item.get("file_path") and item.get("content_sha256")
        and any(effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded"
            and effect.get("effect_type") == "mail_reply_draft"
            and effect.get("target_path") == item["file_path"]
            and effect.get("target_digest") == item["content_sha256"]
            and effect.get("content_sha256") == item["content_sha256"]
            and effect.get("details", {}).get("verified") is True for effect in effects)]
    if len(matches) != 1:
        fail("draft_readback_unavailable")
    artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id, populate_existing=True)
    if (artifact is None or artifact.owner_principal_id != task.owner_principal_id
        or artifact.owner_session_id != task.owner_session_id
        or artifact.bound_task_id != task.task_id or artifact.state not in {"bound", "consumed"}
        or artifact.payload_sha256 != task.typed_input_digest
        or binding_hint["source_input_digest"] != run.input_digest):
        fail("draft_input_changed")
    binding = await db.get(MailMessageBinding, binding_hint["message_binding_id"], populate_existing=True)
    consent = await db.get(MailReadConsent, binding_hint["source_consent_id"], populate_existing=True)
    if (binding is None or consent is None or binding.status != "present"
        or binding.owner_principal_id != task.owner_principal_id
        or binding.owner_session_id != task.owner_session_id
        or binding.message_revision != binding_hint["message_revision"]
        or consent.owner_principal_id != task.owner_principal_id
        or consent.owner_session_id != task.owner_session_id
        or consent.state != "active" or not consent.source_read_allowed
        or utc(consent.expires_at) <= datetime.now(timezone.utc)
        or consent.source_revision != binding_hint["source_consent_revision"]):
        fail("draft_source_changed")
    source_connection = await db.get(GoogleServiceConnection, binding.connection_id, populate_existing=True)
    if (source_connection is None or source_connection.state != "active"
        or source_connection.owner_principal_id != task.owner_principal_id
        or source_connection.owner_session_id != task.owner_session_id
        or source_connection.revision != binding.connection_revision
        or consent.connection_id != binding.connection_id
        or consent.connection_revision != binding.connection_revision
        or consent.goal_id != task.goal_id or consent.goal_revision != task.goal_revision):
        fail("draft_source_changed")
    # No support is silently implied for a foreign evidence-token producer.
    # The existing Mail draft capability does not accept evidence attachments.
    if await db.scalar(select(WorkBoardEvidenceDependency.dependency_id).where(
        WorkBoardEvidenceDependency.task_id == task_id).limit(1)) is not None:
        fail("draft_dependency_unsupported")
    return {"task_id": task_id, "task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "draft_job_id": run.run_identity,
        "draft_job_revision": run.revision, "input_digest": task.typed_input_digest,
        "source_input_digest": run.input_digest,
        "input_artifact_id": artifact.artifact_id, "input_artifact_metadata_digest": artifact.metadata_digest,
        "input_artifact_payload_digest": artifact.payload_sha256,
        "goal_id": task.goal_id, "goal_revision": task.goal_revision,
        "artifact_path": matches[0]["file_path"], "artifact_digest": matches[0]["content_sha256"],
        "message_binding_id": binding.message_binding_id, "binding_revision": binding.revision,
        "message_revision": binding.message_revision,
        "provider_binding_digest": digest([binding.provider_message_id_ciphertext, binding.provider_thread_id_ciphertext]),
        "source_consent_id": consent.consent_id, "source_consent_revision": consent.source_revision,
        "source_consent_digest": consent.source_digest,
        "source_connection": connection_snapshot(source_connection)}


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
    await _assert_canonical_goal_fence(db, goal_id=run.goal_id, goal_revision=run.goal_revision,
        owner_kind=run.owner_kind, owner_principal_id=run.owner_principal_id,
        session_id=run.session_id, authority=run.declared_authority_json)
    if utc(run.deadline_at) <= datetime.now(timezone.utc):
        fail("deadline_expired")
    if lease is not None:
        if lease.job_id != run.run_identity or run.status != "running":
            fail("execution_changed")
        durable_job_repository._assert_lease(run, owner=lease.owner, fencing_token=lease.fencing_token)
    for expected in inputs["connections"]:
        await connection(db, operator, expected)
    if source_required:
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
        or preview.get("mime_digest") != intent.get("mime_digest")
        or preview.get("request_digest") != intent.get("request_digest")
        or datetime.now(timezone.utc).timestamp() >= preview.get("expires_at", 0)
        or intent.get("source_digest") != preview.get("source_digest")
        or not 0 <= datetime.now(timezone.utc).timestamp() - intent.get("source_observed_at", 0) <= 10):
        fail("preview_changed")
    inputs = arguments(run)
    required = {"schema_version": 1, "job_id": run.run_identity,
        "effect_id": run.run_identity+":send-once", "input_digest": run.input_digest,
        "authority_digest": run.authority_digest, "budget_digest": run.budget_digest,
        "original_root": run.operator_session_id, "owner_principal_id": run.owner_principal_id,
        "goal_id": run.goal_id, "goal_revision": run.goal_revision,
        "attempt": run.attempt_count, "fencing_token": lease.fencing_token,
        "approval_id": approval_id, "mime_digest": preview["mime_digest"],
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
    if row.tool_name != "mail.reply.send" or row.fingerprint != preview.get("approval_fingerprint"):
        fail("approval_changed")
    details = json.loads(row.details_json or "{}")
    scope = details.get("approval_scope")
    if (not isinstance(scope, dict) or digest(scope) != preview.get("scope_digest")
        or details.get("scope_digest") != digest(scope)
        or row.fingerprint != fingerprint_tool_call("mail.reply.send", {"scope_digest": digest(scope)})):
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
        "effect_type": "mail_reply_send", "status": "claimed",
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
    effects = json.loads(run.effect_receipts_json)
    if len(effects) != 1 or effects[0].get("effect_id") != intent["effect_id"] or effects[0].get("status") != "claimed":
        fail("effect_changed")
    checkpoint["contact_may_have_occurred"] = True
    checkpoint["dispatch_fence"] = run.fencing_token
    effects[0]["status"] = "unknown"
    await cas(db, run, {"checkpoint_context_json": canonical(checkpoint), "effect_receipts_json": canonical(effects)})


async def append_observation(db, operator, original, auxiliary, *, lease,
    original_revision, original_intent_digest, observation):
    """Observation-only recovery and auxiliary completion in the SAME writer."""
    await authority(db, operator, auxiliary, lease=lease, source_required=False)
    if auxiliary.job_kind != OBSERVATION_KIND or original.job_kind != SEND_KIND:
        fail("recovery_kind_invalid")
    checkpoint = state(original)
    intent = checkpoint.get("intent")
    if (original.owner_principal_id != operator.principal.principal_id
        or original.operator_session_id != operator.session_id
        or original.revision != original_revision or not isinstance(intent, dict)
        or digest(intent) != original_intent_digest
        or original.status not in {"unknown_external_effect", "blocked", "failed", "cancelled"}
        or original.lease_owner is not None or original.lease_expires_at is not None
        or checkpoint.get("transport_quiescent") is not True):
        fail("original_recovery_unavailable")
    original_bindings = {"job_id": original.run_identity, "input_digest": original.input_digest,
        "authority_digest": original.authority_digest, "budget_digest": original.budget_digest,
        "original_root": original.operator_session_id, "owner_principal_id": original.owner_principal_id,
        "goal_id": original.goal_id, "goal_revision": original.goal_revision,
        "attempt": original.attempt_count, "fencing_token": original.fencing_token,
        "effect_id": original.run_identity+":send-once"}
    if any(intent.get(key) != item for key, item in original_bindings.items()):
        fail("original_binding_changed")
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
    inputs = arguments(auxiliary)
    if inputs.get("original_job_id") != original.run_identity or inputs.get("original_intent_digest") != original_intent_digest:
        fail("recovery_input_changed")
    effects = json.loads(original.effect_receipts_json)
    if len(effects) != 1 or effects[0].get("effect_id") != intent["effect_id"] or effects[0].get("status") != "unknown":
        fail("original_liability_changed")
    history = effects[0].get("observation_history", [])
    if not isinstance(history, list) or len(history) >= 16 or any(item.get("auxiliary_job_id") == auxiliary.run_identity for item in history):
        fail("recovery_history_conflict")
    if set(observation) != {"outcome", "response_digest", "private_artifact", "no_learning"}:
        fail("observation_invalid")
    record = {**observation, "schema_version": 1, "auxiliary_job_id": auxiliary.run_identity,
        "effect_id": intent["effect_id"], "original_intent_digest": original_intent_digest,
        "recovery_goal_id": auxiliary.goal_id, "recovery_goal_revision": auxiliary.goal_revision,
        "observed_at": datetime.now(timezone.utc).isoformat()}
    if observation.get("outcome") not in {"verified_sent_observation", "unknown_observation"} or observation.get("no_learning") is not True:
        fail("observation_invalid")
    effects[0]["observation_history"] = [*history, record]
    # Original immutable checkpoint, status, effect status, authority, fence,
    # lease, deadline and Goal are deliberately absent from this update.
    await cas(db, original, {"effect_receipts_json": canonical(effects)}, current_goal=False)
    await durable_job_repository.complete_mail_observation_in_session(db, auxiliary,
        owner=lease.owner, fencing_token=lease.fencing_token, observation=record)
