"""Same-session canonical checks for fixed GitHub recovery receipts.

This module performs no authentication, network, filesystem or decryption work.
The adapter mints its private binding after a positive protected semantic GET;
the job and Board repositories recheck loaded rows in their own transaction.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlmodel import select

from src.db.models import (GitHubFollowthroughConnection, OperatorSession,
    Secret, WorkBoardAttempt, WorkBoardTask)
from src.extensions.github_consent import digest, utc
from src.vault.repository import secret_identity, secret_binding_digest

KINDS = {"github_followthrough_v1": "work.github-followthrough.v1",
    "engineering.repo-publication.v1": "engineering.repo-publication.v1"}


def job_binding(run):
    fields = ("run_identity", "job_kind", "owner_kind", "owner_principal_id",
        "operator_session_id", "session_id", "attempt_count", "fencing_token",
        "input_digest", "authority_digest", "run_fingerprint", "idempotency_scope",
        "idempotency_key", "idempotency_binding", "goal_id", "goal_revision")
    return {key: getattr(run, key) for key in fields}


async def board_binding(db, run):
    attempt = (await db.execute(select(WorkBoardAttempt).where(
        WorkBoardAttempt.workflow_run_id == run.run_identity))).scalars().first()
    if attempt is None:
        if run.idempotency_scope == "work-board-attempt":
            raise ValueError("github_readback_board_attempt_missing")
        return None
    task = (await db.execute(select(WorkBoardTask).where(
        WorkBoardTask.task_id == attempt.task_id))).scalars().first()
    latest = (await db.execute(select(WorkBoardAttempt).where(
        WorkBoardAttempt.task_id == attempt.task_id).order_by(
            WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))).scalars().first()
    if task is None or latest is None or latest.attempt_id != attempt.attempt_id:
        raise ValueError("github_readback_board_attempt_changed")
    return {"task_id": task.task_id, "task_revision": task.task_revision,
        "owner_principal_id": task.owner_principal_id, "owner_session_id": task.owner_session_id,
        "goal_id": task.goal_id, "goal_revision": task.goal_revision,
        "capability_id": task.capability_id, "attempt_id": attempt.attempt_id,
        "fencing_token": attempt.fencing_token, "workflow_run_id": attempt.workflow_run_id}


async def capture_binding(db, run, authority, snapshot):
    return {"schema": "seraph.github-read-revision.v1", "job": job_binding(run),
        "minted_job_revision": run.revision, "original_write_binding": authority.original_binding,
        "read_connection_revision": authority.connection_revision,
        "connection_fence": authority.connection_fence,
        "vault_identity": snapshot.identity, "board": await board_binding(db, run)}


async def check_binding(db, run, binding, *, reserved=True, board=True, ignore_read_revision=False):
    if not isinstance(binding, dict) or binding.get("schema") != "seraph.github-read-revision.v1" or binding.get("job") != job_binding(run):
        raise ValueError("github_readback_canonical_job_changed")
    authority = json.loads(run.declared_authority_json or "{}")
    original = binding.get("original_write_binding")
    native_capability_matches = (run.job_kind in KINDS and
        (authority.get("capability_id") == KINDS[run.job_kind] or
         run.job_kind == "engineering.repo-publication.v1" and "capability_id" not in authority))
    if not native_capability_matches or authority.get("github_consent") != original or run.owner_kind != "user" or run.operator_session_id != original.get("consent_root_id") or run.owner_principal_id != original.get("owner_principal_id"):
        raise ValueError("github_readback_canonical_authority_changed")
    session = await db.get(OperatorSession, run.operator_session_id)
    now = datetime.now(timezone.utc)
    if session is None or session.principal_id != run.owner_principal_id or session.revoked_at is not None or session.replaced_by_id is not None or session.is_bearer_tombstone or utc(session.idle_expires_at) <= now or utc(session.absolute_expires_at) <= now:
        raise ValueError("github_readback_canonical_root_dead")
    connection = await db.get(GitHubFollowthroughConnection, original.get("connection_id"))
    if connection is None or connection.owner_principal_id != run.owner_principal_id or connection.repository != original.get("repository") or not ignore_read_revision and connection.revision != binding.get("read_connection_revision") or connection.mode not in {"active", "disabled", "reconcile_only"}:
        raise ValueError("github_readback_canonical_connection_changed")
    if reserved and (connection.active_job_id != run.run_identity or connection.active_fence != binding.get("connection_fence")):
        raise ValueError("github_readback_canonical_reservation_changed")
    if reserved is False and (connection.active_job_id not in {None, run.run_identity} or connection.active_fence != binding.get("connection_fence")):
        raise ValueError("github_readback_canonical_reservation_changed")
    secret = (await db.execute(select(Secret).where(Secret.key == connection.vault_key,
        Secret.owner_principal_id == run.owner_principal_id, Secret.revoked_at.is_(None)))).scalars().first()
    if secret is None or secret_identity(secret) != binding.get("vault_identity") or secret_binding_digest(secret) != original.get("vault_binding_digest"):
        raise ValueError("github_readback_canonical_credential_changed")
    if board and await board_binding(db, run) != binding.get("board"):
        raise ValueError("github_readback_canonical_board_changed")
    return connection


async def check_persisted_readback(db, run, *, final=False, reserved=True):
    receipt = json.loads(run.github_read_revision_json or "null")
    if not isinstance(receipt, dict) or receipt.get("receipt_digest") != digest({key: value for key, value in receipt.items() if key != "receipt_digest"}):
        raise ValueError("github_readback_persisted_receipt_missing")
    if run.github_capacity_closure_json:
        raise ValueError("github_capacity_already_closed")
    await check_binding(db, run, receipt["binding"], reserved=reserved)
    if final and receipt.get("finalized_job_revision") != run.revision:
        raise ValueError("github_readback_finalized_revision_changed")
    expected = receipt["effect_readback"]
    effects = json.loads(run.effect_receipts_json or "[]")
    if not any(item.get("receipt_kind") == "readback" and item.get("status") == "succeeded"
        and item.get("details", {}).get("verified") is True
        and all(item.get(key) == value for key, value in expected.items()) for item in effects):
        raise ValueError("github_readback_persisted_effect_changed")
    return receipt


async def check_closed_binding(db, run, authority, *, snapshot=None, binding=None):
    """Pure same-session history/Root/credential checks; never reserve or adopt."""
    from src.extensions.github_consent import GitHubClosedReadbackAuthority
    if type(authority) is not GitHubClosedReadbackAuthority or run is None or run.run_identity != authority.job_id or run.job_kind != authority.capability or run.owner_principal_id != authority.principal or run.operator_session_id != authority.root:
        raise ValueError("github_closed_readback_original_job_changed")
    history = json.loads(run.github_capacity_closure_json or "null")
    if not isinstance(history, dict) or history.get("closure_id") != authority.closure_id or history.get("history_digest") != authority.history_digest or history.get("history_digest") != digest({key: value for key, value in history.items() if key != "history_digest"}) or history.get("binding", {}).get("original_write_binding") != authority.original_binding or history.get("binding", {}).get("connection_fence") != authority.connection_fence:
        raise ValueError("github_closed_readback_history_changed")
    current = {**history["binding"], "minted_job_revision": run.revision,
        "read_connection_revision": authority.connection_revision,
        "closure_id": authority.closure_id, "closure_history_digest": authority.history_digest}
    if snapshot is not None and current["vault_identity"] != snapshot.identity:
        raise ValueError("github_closed_readback_credential_changed")
    if binding is not None and current != binding:
        raise ValueError("github_closed_readback_revision_changed")
    await check_binding(db, run, current, reserved=None, board=False)
    return current


async def closed_authority(current, *, expected_revision):
    """Mint from the durable private closure row; no caller history mapping."""
    from src.db import engine
    from src.db.models import WorkflowRunState
    from src.extensions.github_consent import GitHubClosedReadbackAuthority
    async with engine.get_session() as db:
        run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == current["job_id"]))).scalars().one()
        history = json.loads(run.github_capacity_closure_json or "null")
        if not isinstance(history, dict):
            raise ValueError("github_closed_readback_history_missing")
        original = history["binding"]["original_write_binding"]
        authority = GitHubClosedReadbackAuthority(current["owner"]["principal_id"],
            current["operator_session_id"], run.run_identity, run.job_kind,
            expected_revision, history["binding"]["connection_fence"], original,
            history["closure_id"], history["history_digest"])
    await authority.validate()
    return authority


async def record_closed_observation(repository, current, authority, actual, identity):
    """Append one actual positive GET diagnostic; never replace adoption proof."""
    verified_at = actual.observed_at.isoformat()
    readback_id = "github-closed:" + digest({"closure": authority.closure_id,
        "effect": identity["effect_id"], "raw": actual.payload_sha256, "time": verified_at})
    observation = {"schema": "seraph.github-effect-observation.v1", **identity,
        "readback_id": readback_id, "verified_at": verified_at,
        "remote_readback": {"readback_path": actual.readback_path,
            "payload_sha256": actual.payload_sha256, "closure_id": authority.closure_id}}
    content = json.dumps(observation, sort_keys=True, separators=(",", ":")).encode()
    import hashlib
    return await repository.record_github_recovery_observation(current["job_id"],
        read_authority=authority, verified_readback=actual, expected_revision=current["revision"],
        expected_attempt_count=current["attempt_count"], expected_authority_digest=current["authority_digest"],
        **{key: identity[key] for key in ("effect_id", "effect_type", "target_path", "target_digest", "adapter_idempotency_key")},
        readback_id=readback_id, verified_at=verified_at, artifact_content=content,
        artifact_sha256=hashlib.sha256(content).hexdigest())
