"""Durable owner-bound control roots for bounded Gmail reads.

The Gmail source routes are request/response APIs, but their provider boundary
must still have a durable intent, lease, idempotency identity and readback.
This module reuses the canonical durable job repository; it adds no queue or
second scheduler.  Unknown work is never replayed automatically under the
same request key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable, Mapping

from config.settings import settings
from src.integrations.gmail_read import GmailReadError, digest
from src.vault import decrypt, encrypt
from src.workflows.job_runtime import (
    DurableJobAdmissionDenied,
    DurableJobError,
    DurableJobIdempotencyConflict,
    DurableJobIdentity,
    DurableJobSpec,
    durable_job_repository,
)
from src.workspace import canonical_workspace_root


MAIL_SOURCE_VERSION = "mail-source-v1"
MAIL_SOURCE_MAX_SECONDS = 120
_ARTIFACT_RE = __import__("re").compile(r"^artifacts/mail/private/control-[0-9a-f]{32}\.enc$")


class GmailControlError(GmailReadError):
    """Typed durable-control failure."""


@dataclass(frozen=True, slots=True)
class MailSourceRequest:
    operation: str
    owner_principal_id: str
    owner_session_id: str
    connection_id: str
    connection_revision: int
    request_uuid: str
    request_digest: str
    consent_id: str | None = None
    consent_revision: int | None = None
    goal_id: str | None = None
    goal_revision: int | None = None

    @property
    def job_id(self) -> str:
        seed = "|".join(
            (
                self.owner_principal_id,
                self.owner_session_id,
                self.operation,
                self.connection_id,
                self.request_uuid,
            )
        )
        return f"mail-source:{uuid.uuid5(uuid.NAMESPACE_URL, 'seraph:' + seed).hex}"

    @property
    def idempotency_scope(self) -> str:
        return f"mail-source:{self.operation}:{digest(self.connection_id)[:24]}"


@dataclass(frozen=True, slots=True)
class MailSourceLease:
    job_id: str
    owner: str
    fencing_token: int
    revision: int


MailSourceExecutor = Callable[[MailSourceLease], Awaitable[dict[str, Any]]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _artifact_path(job_id: str) -> str:
    return f"artifacts/mail/private/control-{digest(job_id)[:32]}.enc"


def _control_inputs(request: MailSourceRequest) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation": request.operation,
        "connection_id": request.connection_id,
        "connection_revision": request.connection_revision,
        "consent_id": request.consent_id,
        "consent_revision": request.consent_revision,
        "goal_id": request.goal_id,
        "goal_revision": request.goal_revision,
        "request_digest": request.request_digest,
    }


def _authority(request: MailSourceRequest) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "capability_id": "mail.messages.read",
        "capability_version": MAIL_SOURCE_VERSION,
        "principal": request.owner_principal_id,
        "owner_kind": "user",
        "service_id": None,
        "session_id": request.owner_session_id,
        "operator_session_id": request.owner_session_id,
        "operation": request.operation,
        "connection_id": request.connection_id,
        "connection_revision": request.connection_revision,
        "consent_id": request.consent_id,
        "consent_revision": request.consent_revision,
        "goal_id": request.goal_id,
        "goal_revision": request.goal_revision,
        "finite_authority": True,
        "runtime_cap_seconds": MAIL_SOURCE_MAX_SECONDS,
        "budget_microusd": 0,
    }


def _spec(request: MailSourceRequest) -> tuple[DurableJobSpec, str, str]:
    inputs = _control_inputs(request)
    authority = _authority(request)
    input_digest = digest(inputs)
    authority_digest = digest(authority)
    spec = DurableJobSpec(
        identity=DurableJobIdentity(
            job_id=request.job_id,
            owner_kind="user",
            owner_principal_id=request.owner_principal_id,
            job_kind=request.operation,
            capability_version=MAIL_SOURCE_VERSION,
            idempotency_scope=request.idempotency_scope,
            idempotency_key=request.request_uuid,
        ),
        inputs=inputs,
        session_id=request.owner_session_id,
        conversation_id=request.owner_session_id,
        operator_session_id=request.owner_session_id,
        goal_id=request.goal_id,
        goal_revision=request.goal_revision,
        priority=60,
        resource_claims=("mail-read",),
        declared_authority=authority,
        deadline_at=_now() + timedelta(seconds=MAIL_SOURCE_MAX_SECONDS),
        max_attempts=1,
        max_outstanding_jobs=1,
        service_id=None,
        run_fingerprint=input_digest,
        budget_microusd=0,
        budget_digest=digest({"budget_microusd": 0}),
    )
    return spec, input_digest, authority_digest


async def assert_mail_source_lease(lease: MailSourceLease) -> dict[str, Any]:
    """Re-read the durable lease before every contact or private write.

    The ordinary job repository lease check protects owner/fencing state.  The
    additional deadline check is kept here because a valid lease must never
    extend the operation past its finite source deadline.  A cancelled or
    otherwise non-running job fails closed before the next provider request.
    """
    current = await durable_job_repository.assert_active_lease(
        lease.job_id,
        owner=lease.owner,
        fencing_token=lease.fencing_token,
    )
    deadline_raw = current.get("deadline_at") if isinstance(current, Mapping) else None
    try:
        deadline = _as_utc(datetime.fromisoformat(str(deadline_raw).replace("Z", "+00:00")))
    except (TypeError, ValueError, AttributeError) as exc:
        raise GmailControlError(
            "mail_read_reconciliation_required",
            "Mail read deadline metadata requires reconciliation",
            status_code=409,
            recovery_action="reconcile_existing_read",
        ) from exc
    if deadline <= _now():
        raise GmailControlError(
            "mail_read_deadline_expired",
            "The bounded Mail read deadline expired",
            status_code=409,
            recovery_action="reconcile_existing_read",
        )
    return current


def _open_parent(relative_path: str, *, create: bool) -> int | None:
    candidate = PurePosixPath(relative_path)
    if not _ARTIFACT_RE.fullmatch(candidate.as_posix()):
        return None
    try:
        root = canonical_workspace_root(settings.workspace_dir)
    except (OSError, TypeError, ValueError):
        return None
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent: int | None = None
    try:
        parent = os.open(root, flags)
        for component in candidate.parts[:-1]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            next_fd = os.open(component, flags, dir_fd=parent)
            os.close(parent)
            parent = next_fd
        if not stat.S_ISDIR(os.fstat(parent).st_mode):
            return None
        result = parent
        parent = None
        return result
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if parent is not None:
            try:
                os.close(parent)
            except OSError:
                pass


def _prepare_artifact(job_id: str, payload: Mapping[str, Any]) -> tuple[str, bytes, str]:
    plain = json.dumps(dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not 0 < len(plain) <= 16 * 1024:
        raise OSError("mail source result exceeds the bounded artifact limit")
    encrypted = encrypt(plain.decode("utf-8")).encode("utf-8")
    if len(encrypted) > 96 * 1024:
        raise OSError("mail source result exceeds the bounded encrypted artifact limit")
    path = _artifact_path(job_id)
    return path, encrypted, hashlib.sha256(encrypted).hexdigest()


def _publish_artifact(path: str, encrypted: bytes) -> None:
    if not _ARTIFACT_RE.fullmatch(PurePosixPath(path).as_posix()):
        raise OSError("mail source artifact path is invalid")
    parent = _open_parent(path, create=True)
    if parent is None:
        raise OSError("mail source artifact directory is unavailable")
    final_name = PurePosixPath(path).name
    temp_name = f".{final_name}.{hashlib.sha256(encrypted).hexdigest()[:16]}.tmp"
    descriptor: int | None = None
    published = False
    try:
        descriptor = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=parent)
        view = memoryview(encrypted)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("mail source artifact write made no progress")
            view = view[count:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temp_name, final_name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        published = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not published:
            try:
                os.unlink(temp_name, dir_fd=parent)
            except OSError:
                pass
        os.close(parent)


def _write_artifact(job_id: str, payload: Mapping[str, Any]) -> tuple[str, bytes, str]:
    path, encrypted, encrypted_sha = _prepare_artifact(job_id, payload)
    _publish_artifact(path, encrypted)
    return path, encrypted, encrypted_sha


def _read_artifact(job_id: str, expected_sha256: str | None = None) -> dict[str, Any] | None:
    path = _artifact_path(job_id)
    parent = _open_parent(path, create=False)
    if parent is None:
        return None
    descriptor: int | None = None
    try:
        descriptor = os.open(PurePosixPath(path).name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 96 * 1024:
            return None
        encrypted = os.read(descriptor, 96 * 1024 + 1)
        after = os.fstat(descriptor)
        if len(encrypted) > 96 * 1024 or before.st_size != after.st_size or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            return None
        if expected_sha256 and hashlib.sha256(encrypted).hexdigest() != expected_sha256:
            return None
        value = json.loads(decrypt(encrypted.decode("utf-8")))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        os.close(parent)


def _inspect_artifact_file(job_id: str, expected_sha256: str | None) -> str:
    """Return ``absent``, ``verified`` or ``uncertain`` for a private file.

    Deletion is allowed only after this exact deterministic path, regular-file
    identity, bounded size, and recorded ciphertext digest all agree.  A
    permission/socket/filesystem error is deliberately ``uncertain`` rather
    than evidence that the file is absent.
    """
    if not expected_sha256:
        return "uncertain"
    path = _artifact_path(job_id)
    parent = _open_parent(path, create=False)
    if parent is None:
        # _open_parent intentionally collapses path errors.  The caller must
        # reconcile this state instead of treating a daemon/filesystem error as
        # proof of absence.
        return "uncertain"
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                PurePosixPath(path).name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
        except FileNotFoundError:
            return "absent"
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 96 * 1024:
            return "uncertain"
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                return "uncertain"
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        encrypted = b"".join(chunks)
        if before.st_size != after.st_size or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            return "uncertain"
        if hashlib.sha256(encrypted).hexdigest() != expected_sha256:
            return "uncertain"
        return "verified"
    except OSError:
        return "uncertain"
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.close(parent)
        except OSError:
            pass


def _delete_verified_artifact(job_id: str, expected_sha256: str | None) -> str:
    """Delete one deterministic private artifact only after exact verification."""
    state = _inspect_artifact_file(job_id, expected_sha256)
    if state != "verified":
        return state
    path = _artifact_path(job_id)
    parent = _open_parent(path, create=False)
    if parent is None:
        return "uncertain"
    try:
        try:
            os.unlink(PurePosixPath(path).name, dir_fd=parent)
        except FileNotFoundError:
            return "absent"
        os.fsync(parent)
        return "deleted"
    except OSError:
        return "uncertain"
    finally:
        try:
            os.close(parent)
        except OSError:
            pass


async def inspect_mail_source_artifact_recovery(
    job_id: str,
    *,
    owner_principal_id: str,
) -> dict[str, Any]:
    """Inspect a deterministic orphan without mutating it.

    This is the narrow recovery seam used after a crash between filesystem
    publication and the durable artifact receipt.  It returns ``pending`` for
    unknown evidence; callers must not turn an inspection result into an
    automatic delete or replay.
    """
    job = await durable_job_repository.get_job(job_id)
    if not isinstance(job, Mapping):
        return {"status": "unknown", "recovery_action": "reconcile_existing_read"}
    owner = job.get("owner") if isinstance(job.get("owner"), Mapping) else {}
    if owner.get("principal_id") != owner_principal_id:
        return {"status": "blocked", "code": "mail_artifact_owner_mismatch"}
    expected_sha: str | None = None
    checkpoint_found = False
    checkpoints = job.get("checkpoints") if isinstance(job.get("checkpoints"), list) else []
    effects = job.get("effects") if isinstance(job.get("effects"), list) else []
    intent_target = next(
        (
            effect.get("target_digest")
            for effect in effects
            if isinstance(effect, Mapping)
            and effect.get("effect_id") == f"mail-source-effect:{job_id}"
        ),
        None,
    )
    for item in checkpoints:
        if not isinstance(item, Mapping) or item.get("checkpoint_id") != "mail-source-artifact-prepared":
            continue
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            continue
        candidate = payload.get("artifact_sha256")
        if (
            payload.get("job_id") != job_id
            or payload.get("artifact_path") != _artifact_path(job_id)
            or not isinstance(candidate, str)
            or not candidate
            or payload.get("request_digest") != intent_target
            or payload.get("authority_digest") != job.get("authority_digest")
        ):
            continue
        expected_sha = candidate
        checkpoint_found = True
        break
    artifacts = job.get("artifacts") if isinstance(job.get("artifacts"), list) else []
    if not checkpoint_found:
        for item in artifacts:
            if isinstance(item, Mapping) and item.get("artifact_type") == "mail_source_result":
                candidate = item.get("content_sha256")
                if isinstance(candidate, str) and candidate:
                    expected_sha = candidate
                    break
    if expected_sha is None:
        for item in effects:
            details = item.get("details") if isinstance(item, Mapping) else None
            if isinstance(details, Mapping) and details.get("artifact_path") == _artifact_path(job_id):
                candidate = details.get("artifact_sha256")
                if isinstance(candidate, str) and candidate:
                    expected_sha = candidate
                    break
    state = _inspect_artifact_file(job_id, expected_sha)
    pending_state = state in {"verified", "uncertain"} or (checkpoint_found and state == "absent")
    return {
        "status": "pending" if pending_state else state,
        "artifact_state": state,
        "job_id": job_id,
        "owner_principal_id": owner_principal_id,
        "artifact_path": _artifact_path(job_id),
        "artifact_sha256": expected_sha,
        "recovery_action": "reconcile_existing_read" if pending_state else None,
    }


def _artifact_sha(job: Mapping[str, Any]) -> str | None:
    for item in job.get("artifacts", []) if isinstance(job.get("artifacts"), list) else []:
        if isinstance(item, Mapping) and item.get("artifact_type") == "mail_source_result" and item.get("file_path") == _artifact_path(str(job.get("job_id") or "")) and item.get("exists") is True:
            value = item.get("content_sha256")
            if isinstance(value, str) and value:
                return value
    return None


def _verified_result(job: Mapping[str, Any]) -> dict[str, Any] | None:
    if str(job.get("status") or "") != "succeeded":
        return None
    sha = _artifact_sha(job)
    if sha is None:
        return None
    path = _artifact_path(str(job.get("job_id") or ""))
    readback = False
    effects = job.get("effects") if isinstance(job.get("effects"), list) else []
    for item in effects:
        if not isinstance(item, Mapping):
            continue
        details = item.get("details")
        if not isinstance(details, Mapping):
            details = {}
        if (
            item.get("receipt_kind") == "readback"
            and item.get("effect_type") == str(job.get("job_kind") or "")
            and item.get("target_path") == path
            and item.get("status") == "succeeded"
            and item.get("content_sha256") == sha
            and details.get("verified") is True
        ):
            readback = True
            break
    if not readback:
        return None
    return _read_artifact(str(job.get("job_id") or ""), expected_sha256=sha)


async def _prior(request: MailSourceRequest, input_digest: str, authority_digest: str) -> dict[str, Any] | None:
    return await durable_job_repository.get_by_idempotency_binding(
        owner_principal_id=request.owner_principal_id,
        goal_id=request.goal_id,
        goal_revision=request.goal_revision,
        idempotency_scope=request.idempotency_scope,
        idempotency_key=request.request_uuid,
        expected_job_id=request.job_id,
        owner_kind="user",
        service_id=None,
        session_id=request.owner_session_id,
        operator_session_id=request.owner_session_id,
        job_kind=request.operation,
        capability_version=MAIL_SOURCE_VERSION,
        input_digest=input_digest,
        authority_digest=authority_digest,
        run_fingerprint=input_digest,
    )


async def _settle_unknown_after_intent(
    request: MailSourceRequest,
    *,
    lease_owner: str,
    fencing_token: int,
    artifact_path: str | None = None,
    artifact_sha256: str | None = None,
) -> bool:
    """Persist an uncertain post-intent outcome using the original fence.

    Recovery deliberately never reclaims or re-reads a lease for this write.
    The owner/fencing token captured at the intent boundary is the only
    authority that may settle that attempt; a later worker must reconcile it
    through the durable job repository instead of being adopted by a stale
    cancellation handler.
    """
    current = await durable_job_repository.get_job(request.job_id)
    if not isinstance(current, Mapping) or current.get("status") != "running":
        return False
    revision = int(current.get("revision") or 0)
    details: dict[str, Any] = {
        "operation": request.operation,
        "recovery_action": "reconcile_existing_read",
    }
    if artifact_path == _artifact_path(request.job_id) and artifact_sha256:
        details.update(
            {
                "artifact_path": artifact_path,
                "artifact_sha256": artifact_sha256,
                "artifact_recovery": "pending_exact_owner_job_hash_reconciliation",
            }
        )
    await durable_job_repository.record_effect(
        request.job_id,
        effect_type=request.operation,
        effect_id=f"mail-source-effect:{request.job_id}",
        target_path=_artifact_path(request.job_id),
        target_digest=request.request_digest,
        adapter_idempotency_key=request.request_uuid,
        status="unknown",
        details=details,
        owner=lease_owner,
        fencing_token=fencing_token,
        expected_revision=revision,
    )
    current = await durable_job_repository.get_job(request.job_id)
    if not isinstance(current, Mapping) or current.get("status") != "running":
        return False
    await durable_job_repository.transition_job(
        request.job_id,
        "unknown_external_effect",
        owner=lease_owner,
        fencing_token=fencing_token,
        expected_revision=int(current.get("revision") or 0),
        reason="mail_read_reconciliation_required",
    )
    return True


async def run_mail_source_control(request: MailSourceRequest, execute: MailSourceExecutor) -> dict[str, Any]:
    """Admit, claim, execute, artifact/read back, and settle one Mail read."""

    spec, input_digest, authority_digest = _spec(request)
    try:
        prior = await _prior(request, input_digest, authority_digest)
    except DurableJobIdempotencyConflict as exc:
        raise GmailControlError("mail_idempotency_conflict", "The Mail request key is bound to another request", status_code=409, recovery_action="use_new_request_uuid") from exc
    if prior is not None:
        result = _verified_result(prior)
        if result is not None:
            return result
        raise GmailControlError("mail_read_reconciliation_required", "The same Mail request requires reconciliation", status_code=409, recovery_action="reconcile_existing_read")
    try:
        admission = await durable_job_repository.admit_job(spec)
    except DurableJobAdmissionDenied as exc:
        raise GmailControlError("mail_read_admission_blocked", "Mail read admission is currently blocked", status_code=409, recovery_action="wait_or_reconcile_existing_work") from exc
    except DurableJobIdempotencyConflict as exc:
        raise GmailControlError("mail_idempotency_conflict", "The Mail request key is bound to another request", status_code=409, recovery_action="use_new_request_uuid") from exc
    except DurableJobError as exc:
        raise GmailControlError("mail_read_reconciliation_required", "Mail read admission requires reconciliation", status_code=409, recovery_action="reconcile_existing_read") from exc
    if admission.get("receipt", {}).get("status") == "deduped":
        replay = await _prior(request, input_digest, authority_digest)
        result = _verified_result(replay or {})
        if result is None:
            raise GmailControlError("mail_read_reconciliation_required", "The same Mail request requires reconciliation", status_code=409, recovery_action="reconcile_existing_read")
        return result
    lease_owner = f"mail-source:{request.owner_principal_id}:{request.job_id}"
    fencing_token: int | None = None
    effect_intent_recorded = False
    published_artifact_path: str | None = None
    published_artifact_sha256: str | None = None
    try:
        queued = await durable_job_repository.queue_job(request.job_id, expected_state="accepted", expected_revision=int(admission.get("revision") or 0))
        claimed = await durable_job_repository.claim_job(request.job_id, owner=lease_owner, lease_seconds=MAIL_SOURCE_MAX_SECONDS, expected_state="queued", expected_revision=int(queued.get("revision") or 0))
        lease = claimed.get("lease") if isinstance(claimed.get("lease"), Mapping) else {}
        fencing_token = lease.get("fencing_token")
        if type(fencing_token) is not int or fencing_token <= 0:
            raise GmailControlError("mail_read_reconciliation_required", "Mail read admission requires reconciliation", status_code=409, recovery_action="reconcile_existing_read")
        await assert_mail_source_lease(MailSourceLease(request.job_id, lease_owner, fencing_token, int(claimed.get("revision") or 0)))
        intent = await durable_job_repository.record_effect(request.job_id, effect_type=request.operation, effect_id=f"mail-source-effect:{request.job_id}", target_path=_artifact_path(request.job_id), target_digest=request.request_digest, adapter_idempotency_key=request.request_uuid, status="intent", details={"operation": request.operation}, owner=lease_owner, fencing_token=fencing_token, expected_revision=int(claimed.get("revision") or 0))
        effect_intent_recorded = True
        lease_obj = MailSourceLease(request.job_id, lease_owner, fencing_token, int(intent.get("revision") or 0))
        await assert_mail_source_lease(lease_obj)
        try:
            remaining = (_as_utc(datetime.fromisoformat(str(claimed.get("deadline_at")).replace("Z", "+00:00"))) - _now()).total_seconds()
        except (TypeError, ValueError, AttributeError) as exc:
            raise GmailControlError("mail_read_reconciliation_required", "Mail read deadline metadata requires reconciliation", status_code=409, recovery_action="reconcile_existing_read") from exc
        if remaining <= 0:
            raise GmailControlError("mail_read_deadline_expired", "The bounded Mail read deadline expired", status_code=409, recovery_action="reconcile_existing_read")
        async with asyncio.timeout(remaining):
            payload = await execute(lease_obj)
            if not isinstance(payload, dict):
                raise GmailControlError("mail_read_reconciliation_required", "Mail read returned an invalid result", status_code=409, recovery_action="reconcile_existing_read")
            await assert_mail_source_lease(lease_obj)
            path, encrypted, encrypted_sha = await asyncio.to_thread(_prepare_artifact, request.job_id, payload)
            checkpoint_state = {
                "job_id": request.job_id,
                "artifact_path": path,
                "artifact_sha256": encrypted_sha,
                "request_digest": request.request_digest,
                "authority_digest": authority_digest,
            }
            checkpoint = await durable_job_repository.record_checkpoint(
                request.job_id,
                checkpoint_id="mail-source-artifact-prepared",
                state=checkpoint_state,
                checkpoint_payload=checkpoint_state,
                owner=lease_obj.owner,
                fencing_token=lease_obj.fencing_token,
                safe=True,
                expected_revision=lease_obj.revision,
            )
            lease_obj = MailSourceLease(
                request.job_id,
                lease_obj.owner,
                lease_obj.fencing_token,
                int(checkpoint.get("revision") or 0),
            )
            await assert_mail_source_lease(lease_obj)
            await asyncio.to_thread(_publish_artifact, path, encrypted)
            published_artifact_path = path
            published_artifact_sha256 = encrypted_sha
            await assert_mail_source_lease(lease_obj)
            artifact = await durable_job_repository.record_artifact(request.job_id, file_path=path, artifact_type="mail_source_result", content=encrypted, owner=lease_owner, fencing_token=fencing_token, expected_revision=lease_obj.revision)
            await assert_mail_source_lease(lease_obj)
            readback = await durable_job_repository.record_readback(request.job_id, target_path=path, status="succeeded", effect_id=f"mail-source-effect:{request.job_id}", effect_type=request.operation, target_digest=request.request_digest, content_sha256=encrypted_sha, readback_id=f"mail-source-readback:{request.job_id}", verified_at=_now().isoformat().replace("+00:00", "Z"), details={"verified": True, "memory_status": "no_learning"}, owner=lease_owner, fencing_token=fencing_token, expected_revision=int(artifact.get("revision") or 0))
            await assert_mail_source_lease(lease_obj)
            await durable_job_repository.transition_job(request.job_id, "succeeded", owner=lease_owner, fencing_token=fencing_token, expected_revision=int(readback.get("revision") or 0), result_summary="Mail source read completed")
            return payload
    except GmailReadError as exc:
        # The effect intent is recorded before the provider callback.  Once
        # that boundary has been crossed, even a typed adapter failure may be
        # ambiguous to the caller (for example a timeout after request write),
        # so preserve the durable unknown state instead of allowing a same-key
        # provider replay.  Errors raised before intent retain their typed
        # public response because no external contact was admitted.
        if fencing_token is None or not effect_intent_recorded:
            raise
        try:
            await _settle_unknown_after_intent(
                request,
                lease_owner=lease_owner,
                fencing_token=fencing_token,
                artifact_path=published_artifact_path,
                artifact_sha256=published_artifact_sha256,
            )
        except Exception:
            # The original typed error is retained for transport/authority
            # failures if the recovery projection itself cannot be persisted;
            # the durable row remains non-replayable and is surfaced by the
            # next reconciliation read.
            pass
        raise GmailControlError(
            "mail_read_reconciliation_required",
            "Mail read requires reconciliation",
            status_code=409,
            recovery_action="reconcile_existing_read",
        ) from exc
    except asyncio.CancelledError as exc:
        if fencing_token is not None and effect_intent_recorded:
            try:
                await asyncio.wait_for(
                    asyncio.shield(
                        _settle_unknown_after_intent(
                            request,
                            lease_owner=lease_owner,
                            fencing_token=fencing_token,
                            artifact_path=published_artifact_path,
                            artifact_sha256=published_artifact_sha256,
                        )
                    ),
                    timeout=5.0,
                )
            except Exception:
                # Preserve the original cancellation.  A failed recovery is
                # still operator-visible through the unresolved durable job.
                pass
        raise
    except Exception as exc:
        # The intent is already durable.  Do not let a timeout, cancellation,
        # or storage error silently turn into a same-key provider replay.
        try:
            await _settle_unknown_after_intent(
                request,
                lease_owner=lease_owner,
                fencing_token=fencing_token,
                artifact_path=published_artifact_path,
                artifact_sha256=published_artifact_sha256,
            )
        except Exception:
            pass
        raise GmailControlError("mail_read_reconciliation_required", "Mail read requires reconciliation", status_code=409, recovery_action="reconcile_existing_read") from exc


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


__all__ = [
    "GmailControlError",
    "MailSourceLease",
    "MailSourceRequest",
    "MAIL_SOURCE_MAX_SECONDS",
    "MAIL_SOURCE_VERSION",
    "assert_mail_source_lease",
    "run_mail_source_control",
]
