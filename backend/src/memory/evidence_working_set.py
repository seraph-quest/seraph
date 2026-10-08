"""Task-bound, canonical evidence. Derived packets contain references only.

No advisory index is authority, no retrieval performs inference, and private
source read permission does not grant strategist/model egress permission.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, or_, select

from config.settings import settings
from src.artifacts.registry import artifact_id_for
from src.db.models import (
    CalendarEventBinding, CalendarPrepReceipt, Goal, GuardianSourceWatch, Memory, MemoryProposal, MemorySource, MemoryStatus,
    WorkBoardAttempt, WorkBoardEvent, WorkBoardStatus, WorkBoardTask, WorkflowRunState,
)
from src.memory.hybrid_retrieval import _query_terms, _term_overlap_score, _recency_boost
from src.memory.repository import (
    _canonical_memory_deletion_marker, _canonical_memory_without_tombstone_clause,
    _memory_record_owner_validated_provenance,
    _ordinary_model_memory_clause,
)
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError, WorkBoardRepository, _begin_sqlite_immediate
from src.workspace import canonical_workspace_root

MAX_SOURCES = 32
MAX_CLAIMS = 16
MAX_BYTES = 64 * 1024
_PUBLIC_TYPES = frozenset({
    "browser_public_task_result", "guardian_decision_dossier", "guardian_local_task",
    "markdown_document", "goal_snapshot", "research_report", "document_summary",
    "evidence_dossier", "evidence_local_report",
})
_TOKEN = re.compile(r"^[a-z0-9]{64}$")


class EvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_task_revision: int = Field(ge=1)
    expected_packet_revision: int = Field(ge=0)
    query: str | None = Field(default=None, max_length=200)


class EvidenceExclusionRequest(EvidenceRequest):
    excluded_source_ids: list[str] = Field(max_length=32)


class EvidenceAdoptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_task_revision: int = Field(ge=1)
    expected_packet_revision: int = Field(ge=1)
    expected_packet_digest: str = Field(pattern="^[a-f0-9]{64}$")
    allow_model_context: bool


class EvidenceCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str = Field(pattern="^[a-f0-9]{64}$")
    source_digest: str = Field(pattern="^[a-f0-9]{64}$")
    span_digest: str = Field(pattern="^[a-f0-9]{64}$")
    version: str = Field(min_length=1, max_length=64)
    line_start: int = Field(ge=1, le=65536)
    line_end: int = Field(ge=1, le=65536)


class EvidenceClaim(EvidenceCitation):
    text: str = Field(max_length=1000)
    source_kind: str
    owner_session_id: str
    owner_principal_id: str
    confidence: float | None
    freshness: str
    updated_at: str
    memory_id: str | None
    model_context_allowed: bool
    private_source: bool = False
    page: int | None = None
    row: int | None = None
    ownership_access: str = "current"


class EvidencePacketResponse(BaseModel):
    revision: int
    digest: str | None
    task_id: str | None = None
    goal_id: str | None = None
    query: str | None = None
    claims: list[EvidenceClaim]
    excluded_source_ids: list[str]
    allow_model_context: bool
    invalidated_count: int
    blocked_sources: list[str]
    mode: str
    reason: str
    memory_status: str
    ownership_access: str = "current"
    execution_block_reason: str | None = None


def _digest(value: Any) -> str:
    payload = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _load(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (ValueError, TypeError):
        return fallback


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _source_id(kind: str, identifier: str) -> str:
    return _digest({"kind": kind, "identifier": identifier})


def _read_file(relative: str, expected_digest: str) -> bytes:
    """Bounded no-follow traversal, including workspace and ancestor checks."""
    parts = Path(relative).parts
    if not parts or parts[0] != "artifacts" or any(p in {"", ".", ".."} for p in parts):
        raise OSError("unsafe artifact reference")
    root = canonical_workspace_root(settings.workspace_dir)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        try:
            before = os.fstat(file_descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
                raise OSError("unbounded or unsafe artifact")
            data = b""
            while len(data) <= MAX_BYTES:
                chunk = os.read(file_descriptor, min(8192, MAX_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            after = os.fstat(file_descriptor)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise OSError("artifact changed while reading")
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)
    if len(data) > MAX_BYTES or _digest(data) != expected_digest:
        raise OSError("artifact digest drift")
    return data


def _packet_path(task: WorkBoardTask, revision: int, digest: str) -> str:
    owner_key = _digest([task.owner_principal_id, task.owner_session_id])
    task_key = _digest(task.task_id)
    return f"artifacts/work-board/evidence/{owner_key}/{task_key}/{revision}-{digest}.json"


def _write_packet(task: WorkBoardTask, packet: dict[str, Any]) -> str:
    data = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    if len(data) > MAX_BYTES:
        raise BoardError("evidence_too_large", "The evidence packet exceeds its finite limit", status_code=409)
    digest = _digest(data)
    relative = _packet_path(task, packet["revision"], digest)
    root = canonical_workspace_root(settings.workspace_dir)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parts = Path(relative).parts
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        temporary = f".packet-{secrets.token_hex(16)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, parts[-1], src_dir_fd=descriptor, dst_dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except FileNotFoundError:
                pass
    finally:
        os.close(descriptor)
    return digest


async def _task(db, owner: WorkBoardOwner, task_id: str) -> WorkBoardTask:
    task = await WorkBoardRepository().get_task(db, owner, task_id)
    await WorkBoardRepository()._validate_goal(db, owner, goal_id=task.goal_id, goal_revision=task.goal_revision)
    return task


async def _latest(db, task: WorkBoardTask) -> dict[str, Any] | None:
    event = (await db.execute(select(WorkBoardEvent).where(
        WorkBoardEvent.task_id == task.task_id,
        WorkBoardEvent.owner_principal_id == task.owner_principal_id,
        WorkBoardEvent.owner_session_id == task.owner_session_id,
        WorkBoardEvent.kind == "task.evidence.updated",
    ).order_by(WorkBoardEvent.event_id.desc()).limit(1))).scalar_one_or_none()
    if event is None:
        return None
    metadata = _load(event.metadata_json, {})
    revision, digest = metadata.get("packet_revision"), metadata.get("packet_digest")
    if type(revision) is not int or revision < 1 or not isinstance(digest, str) or not _TOKEN.fullmatch(digest):
        raise BoardError("evidence_packet_invalid", "The packet requires regeneration", status_code=409)
    try:
        packet = json.loads(_read_file(_packet_path(task, revision, digest), digest))
    except (OSError, ValueError) as exc:
        raise BoardError("evidence_packet_unavailable", "Regenerate the task evidence packet", status_code=409) from exc
    if packet.get("task_id") != task.task_id or packet.get("goal_id") != task.goal_id or packet.get("revision") != revision:
        raise BoardError("evidence_packet_invalid", "The packet binding changed", status_code=409)
    if (not isinstance(packet.get("citations"), list) or len(packet["citations"]) > MAX_CLAIMS
        or not isinstance(packet.get("query"), str) or len(packet["query"]) > 200
        or type(packet.get("allow_model_context")) is not bool
        or not isinstance(packet.get("excluded_source_ids"), list)
        or len(packet["excluded_source_ids"]) > MAX_SOURCES):
        raise BoardError("evidence_packet_invalid", "The packet schema requires regeneration", status_code=409)
    try:
        packet["citations"] = [EvidenceCitation.model_validate(citation).model_dump() for citation in packet["citations"]]
    except ValueError as exc:
        raise BoardError("evidence_packet_invalid", "The packet citations require regeneration", status_code=409) from exc
    return {**packet, "digest": digest}


async def _read_task(db, owner: WorkBoardOwner, task_id: str, operator=None):
    return await WorkBoardRepository().read_context_task(db, owner, task_id, operator=operator)


def _source(kind: str, identifier: str, text: str, *, digest: str, version: str,
            owner: WorkBoardOwner, updated_at: datetime, confidence: float | None = None,
            memory_id: str | None = None, private: bool = False) -> dict[str, Any]:
    return {"source_id": _source_id(kind, identifier), "source_kind": kind,
            "identifier": identifier, "text": text[:MAX_BYTES], "source_digest": digest,
            "version": version, "owner_session_id": owner.session_id,
            "owner_principal_id": owner.principal_id, "confidence": confidence,
            "updated_at": _utc(updated_at).isoformat(), "memory_id": memory_id,
            "model_context_allowed": not private, "private_source": private}


async def _memory_sources(db, owner: WorkBoardOwner, task: WorkBoardTask,
                          selected_ids: set[str] | None = None) -> list[dict[str, Any]]:
    goal = await db.get(Goal, task.goal_id)
    safe_metadata = case((func.json_valid(Memory.metadata_json) == 1, Memory.metadata_json), else_="{}")
    statement = select(Memory).where(
        Memory.source_session_id == owner.session_id,
        Memory.status == MemoryStatus.active,
        _canonical_memory_without_tombstone_clause(),
        _ordinary_model_memory_clause(),
        func.length(Memory.content) <= MAX_BYTES,
        func.length(func.coalesce(Memory.metadata_json, "")) <= MAX_BYTES,
        or_(
            func.json_extract(safe_metadata, "$.goal_id") == task.goal_id,
            func.json_extract(safe_metadata, "$.provenance.goal_id") == task.goal_id,
            func.json_extract(safe_metadata, "$.work_board_provenance.goal_id") == task.goal_id,
            Memory.id.in_(select(MemoryProposal.accepted_memory_id).where(
                MemoryProposal.goal_id == task.goal_id, MemoryProposal.owner_principal_id == owner.principal_id,
                MemoryProposal.owner_session_id == owner.session_id, MemoryProposal.status == "accepted")),
        ),
    )
    if selected_ids is not None:
        statement = statement.where(Memory.id.in_(selected_ids))
    rows = list((await db.execute(statement.order_by(Memory.updated_at.desc()).limit(128))).scalars().all())
    result = []
    for memory in rows:
        if selected_ids is not None and memory.id not in selected_ids:
            continue
        metadata = _load(memory.metadata_json, {})
        if not isinstance(metadata, dict):
            continue
        scope = metadata.get("work_board_provenance", metadata.get("provenance", metadata))
        if not isinstance(scope, dict):
            continue
        # Text similarity alone never broadens the selected project scope.
        if scope.get("goal_id") != task.goal_id and metadata.get("goal_id") != task.goal_id:
            accepted = (await db.execute(select(MemoryProposal).where(
                MemoryProposal.accepted_memory_id == memory.id,
                MemoryProposal.owner_session_id == owner.session_id,
                MemoryProposal.owner_principal_id == owner.principal_id,
                MemoryProposal.goal_id == task.goal_id,
                MemoryProposal.status == "accepted",
            ).limit(1))).scalar_one_or_none()
            if accepted is None:
                continue
        if _canonical_memory_deletion_marker(memory) is not None:
            continue
        sources = list((await db.execute(select(MemorySource).where(MemorySource.memory_id == memory.id))).scalars().all())
        if not sources or any(s.source_session_id != owner.session_id for s in sources):
            continue
        if any(s.source_type == "work_board_m5" for s in sources):
            provenance = await _memory_record_owner_validated_provenance(
                db, memory, owner_session_id=owner.session_id, sources=sources)
            if provenance.get("verified_source") is not True:
                continue
        elif not any(s.source_type == "operator" for s in sources):
            continue  # Unreviewed observations and model suggestions stay unknown.
        if not isinstance(metadata, dict) or goal is None:
            continue
        safe = await WorkBoardRepository._safe_text(memory.content[:MAX_BYTES], db=db)
        if safe == "[redaction unavailable]":
            continue
        result.append(_source("canonical_memory", memory.id, safe, digest=_digest(memory.content.encode()),
                              version=_utc(memory.updated_at).isoformat(), owner=owner,
                              updated_at=memory.last_confirmed_at or memory.updated_at,
                              confidence=memory.confidence, memory_id=memory.id,
                              private=metadata.get("privacy_boundary", "operator_visible") != "operator_visible"))
    return result


def _verified_receipts(run: WorkflowRunState) -> list[dict[str, Any]]:
    artifacts, effects = _load(run.artifact_receipts_json, []), _load(run.effect_receipts_json, [])
    if not isinstance(artifacts, list) or not isinstance(effects, list):
        return []
    result = []
    for receipt in artifacts[:MAX_SOURCES]:
        if not isinstance(receipt, dict) or receipt.get("exists") is not True:
            continue
        path, digest = receipt.get("file_path"), receipt.get("content_sha256")
        if not isinstance(path, str) or not isinstance(digest, str) or not _TOKEN.fullmatch(digest):
            continue
        expected_id = artifact_id_for(file_path=path, artifact_type=str(receipt.get("artifact_type") or ""),
                                      producer=str(receipt.get("producer") or ""),
                                      run_id=run.run_identity, content_sha256=digest)
        # DurableJobRepository receipts bind the run through their containing
        # canonical row; their safe projection intentionally omits run_id.
        if receipt.get("artifact_id") != expected_id or receipt.get("run_id") not in {None, run.run_identity}:
            continue
        if receipt.get("producer") != run.job_kind:
            continue
        if not any(isinstance(e, dict) and e.get("receipt_kind") == "readback"
                   and e.get("status") == "succeeded" and e.get("target_path") == path
                   and e.get("target_digest") == digest and e.get("content_sha256") == digest
                   and isinstance(e.get("details"), dict) and e["details"].get("verified") is True
                   for e in effects[:128]):
            continue
        result.append(receipt)
    return result


async def _private_source(db, owner: WorkBoardOwner, source_task: WorkBoardTask,
                          run: WorkflowRunState, receipt: dict[str, Any]) -> str | None:
    """Reuse capability-specific source permission/readers; never new egress."""
    kind = receipt["artifact_type"]
    if kind == "mail_reply_draft":
        from src.api.mail import _binding_for, _consent_for, _connection_for, _validate_source_scope
        from src.work_board.input_artifacts import resolve_input_artifact_for_copy
        from src.workflows.mail_reply_draft import artifact_path_for_job, read_private_draft
        if source_task.capability_id != "work.mail-reply-draft.v1" or run.job_kind != "mail_reply_draft":
            return None
        resolved = await resolve_input_artifact_for_copy(db, owner,
            typed_input_ref=source_task.typed_input_ref,
            typed_input_digest=source_task.typed_input_digest,
            capability_id=source_task.capability_id,
            goal_id=source_task.goal_id, goal_revision=source_task.goal_revision)
        inputs = resolved.input
        consent = await _consent_for(db, owner, inputs["mail_consent_id"])
        connection = await _connection_for(db, owner, consent.connection_id)
        await _validate_source_scope(db, owner, connection=connection, consent=consent,
                                    expected_source_revision=inputs["expected_source_consent_revision"])
        binding = await _binding_for(db, owner, inputs["message_binding_id"], connection.connection_id)
        if (binding.status != "present" or binding.message_revision != inputs["expected_message_revision"]
            or binding.source_consent_id != consent.consent_id
            or binding.source_consent_revision != consent.source_revision
            or binding.connection_revision != connection.revision):
            return None
        if receipt["file_path"] != artifact_path_for_job(run.run_identity):
            return None
        payload = read_private_draft(receipt["file_path"], receipt["content_sha256"])
        return "\n".join([str(payload.get("subject") or ""), str(payload.get("plainbody") or "")])
    if kind == "calendar_meeting_prep_result":
        from src.api.calendar import _active_consent
        from src.integrations.google_calendar import calendar_artifact_path_for_job, read_calendar_result_bytes
        prep = (await db.execute(select(CalendarPrepReceipt).where(
            CalendarPrepReceipt.durable_job_id == run.run_identity,
            CalendarPrepReceipt.task_id == source_task.task_id,
            CalendarPrepReceipt.owner_principal_id == owner.principal_id,
            CalendarPrepReceipt.owner_session_id == owner.session_id,
            CalendarPrepReceipt.status == "succeeded",
        ))).scalar_one_or_none()
        if prep is None or source_task.capability_id != "calendar.meeting-prep.v1":
            return None
        consent, connection = await _active_consent(db, owner, prep.consent_id)
        if consent.revision != prep.consent_revision or connection.revision != prep.connection_revision:
            return None
        binding = await db.get(CalendarEventBinding, prep.event_binding_id)
        if (binding is None or binding.owner_principal_id != owner.principal_id
            or binding.owner_session_id != owner.session_id or binding.state != "selected"
            or binding.consent_id != consent.consent_id or binding.consent_revision != consent.revision
            or binding.connection_id != connection.connection_id or binding.connection_revision != connection.revision
            or binding.event_revision != prep.event_revision_read_2):
            return None
        if receipt["file_path"] != calendar_artifact_path_for_job(run.run_identity):
            return None
        if (prep.artifact_id != receipt["artifact_id"] or prep.file_path != receipt["file_path"]
            or prep.content_sha256 != receipt["content_sha256"]):
            return None
        data = read_calendar_result_bytes(receipt["file_path"])
        if data is None or _digest(data) != receipt["content_sha256"]:
            return None
        payload = json.loads(data)
        return "\n".join(str(payload.get(field) or "") for field in
                         ("summary", "agenda", "questions", "risks", "preparation_steps"))
    return None


async def _artifact_sources(db, owner: WorkBoardOwner, task: WorkBoardTask,
                            selected_task_ids: set[str] | None = None,
                            selected_artifact_ids: set[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    sources, blocked = [], []
    candidate_count = 0
    rows = (await db.execute(select(WorkBoardTask, WorkBoardAttempt, WorkflowRunState).join(
        WorkBoardAttempt, WorkBoardAttempt.task_id == WorkBoardTask.task_id).join(
        WorkflowRunState, WorkflowRunState.run_identity == WorkBoardAttempt.workflow_run_id).where(
        WorkBoardTask.owner_principal_id == owner.principal_id,
        WorkBoardTask.owner_session_id == owner.session_id,
        WorkBoardTask.goal_id == task.goal_id,
        WorkBoardTask.goal_revision == task.goal_revision,
        WorkBoardTask.archived_at.is_(None),
        WorkflowRunState.owner_kind.in_(["user", "service"]),
        WorkflowRunState.operator_session_id == owner.session_id,
        WorkflowRunState.goal_id == task.goal_id,
        WorkflowRunState.goal_revision == WorkBoardTask.goal_revision,
        WorkflowRunState.status == "succeeded",
    ).order_by(WorkflowRunState.updated_at.desc()).limit(MAX_SOURCES))).all()
    for source_task, _attempt, run in rows:
        from src.memory.evidence_sources import verified_task_outputs
        if selected_task_ids is not None and source_task.task_id not in selected_task_ids:
            continue
        for source_run, receipt in await verified_task_outputs(db, source_task, _attempt, run):
            if selected_artifact_ids is not None and receipt["artifact_id"] not in selected_artifact_ids:
                continue
            if candidate_count >= MAX_SOURCES:
                return sources, sorted(set(blocked))[:MAX_SOURCES]
            candidate_count += 1
            kind = receipt["artifact_type"]
            private = kind in {"mail_reply_draft", "calendar_meeting_prep_result", "moltbook_private_browser_read", "forgejo_private_transaction"}
            try:
                if source_task.capability_id == "guardian.research-watch.v1":
                    from src.work_board.dispatcher import _parse_typed_input
                    inputs = _parse_typed_input(source_task)
                    watch = await db.get(GuardianSourceWatch, inputs.get("watch_id"))
                    if (watch is None or watch.state != "active"
                        or watch.owner_principal_id != owner.principal_id or watch.owner_session_id != owner.session_id
                        or watch.goal_id != task.goal_id or watch.plan_revision != inputs.get("expected_plan_revision")):
                        raise OSError("research source permission changed")
                if private:
                    text = await _private_source(db, owner, source_task, run, receipt)
                    if text is None:
                        blocked.append(f"{kind}:source_permission_or_readback_required")
                        continue
                elif kind in _PUBLIC_TYPES:
                    text = _read_file(receipt["file_path"], receipt["content_sha256"]).decode("utf-8")
                    # Browser result JSON contains only the existing public result
                    # summary, not raw page content, cookies or runtime metadata.
                    if kind == "browser_public_task_result":
                        from src.browser.task_runner import browser_artifact_path_for_job
                        from src.security.site_policy import evaluate_site_access
                        if source_task.capability_id != "browser.public-task.v1" or receipt["file_path"] != browser_artifact_path_for_job(run.run_identity):
                            raise OSError("browser artifact identity drift")
                        payload = json.loads(text)
                        if payload.get("task_id") != source_task.task_id or payload.get("attempt_id") != _attempt.attempt_id:
                            raise OSError("browser source task binding drift")
                        if not evaluate_site_access(str(payload.get("final_url") or ""), resolve_dns=False).allowed:
                            raise OSError("browser site permission changed")
                        text = "\n".join(str(item.get("value") or "") for item in payload.get("extracts", [])[:32]
                                         if isinstance(item, dict) and item.get("kind") == "extract")
                else:
                    blocked.append(f"{kind}:unsupported_source_kind")
                    continue
                text = await WorkBoardRepository._safe_text(text[:MAX_BYTES], db=db)
                if text == "[redaction unavailable]":
                    blocked.append(f"{kind}:redaction_unavailable")
                    continue
                source = _source(kind, receipt["artifact_id"], text,
                    digest=receipt["content_sha256"], version=str(source_run.revision), owner=owner,
                    updated_at=source_run.updated_at, private=private)
                # Private server-only lineage for exact execution-token resolution.
                # Rendered packet claims keep their existing explicit projection.
                source["canonical_binding"] = {
                    "task_id": source_task.task_id, "attempt_id": _attempt.attempt_id,
                    "run_id": source_run.run_identity,
                }
                sources.append(source)
            except Exception:
                blocked.append(f"{kind}:source_revoked_or_unavailable")
    return sources, sorted(set(blocked))[:MAX_SOURCES]


async def _sources(db, owner: WorkBoardOwner, task: WorkBoardTask, operator=None):
    recovered = operator is not None and operator.session_id != owner.session_id
    artifacts, blocked, sources = [], [], []
    if not recovered:
        artifacts, blocked = await _artifact_sources(db, owner, task)
        sources = [*await _memory_sources(db, owner, task), *artifacts]
    if operator is not None:
        # Stable ownership selects exact rows, never authenticates retired roots.
        # A current task additionally needs the explicit fresh-work journal link.
        try:
            from src.auth.ownership import selected_read_scopes, fresh_work_source_scope
        except ImportError:
            return sources[:128], [*blocked, "historical_evidence:identity_read_scope_unavailable"]
        selected = {kind: await selected_read_scopes(operator, kind, db=db)
                    for kind in ("goal", "task", "output_artifact", "memory")}
        if recovered:
            selected_ids = {record_id for record_id, root in selected["memory"].items() if root == owner.session_id}
            selected_tasks = {record_id for record_id, root in selected["task"].items() if root == owner.session_id}
            selected_outputs = {record_id for record_id, root in selected["output_artifact"].items() if root == owner.session_id}
            records = await _memory_sources(db, owner, task, selected_ids=selected_ids)
            outputs, blocked = await _artifact_sources(db, owner, task,
                selected_task_ids=selected_tasks, selected_artifact_ids=selected_outputs)
            for source in [*records, *outputs]:
                source["model_context_allowed"] = False
                source["ownership_access"] = "recovered_read_only"
            return [*records, *outputs], blocked
        historical = []
        for goal_id, root in list(selected["goal"].items())[:MAX_SOURCES]:
            if root == owner.session_id:
                continue
            if await fresh_work_source_scope(operator, task.task_id, "goal", goal_id, db=db) != root:
                continue
            source_goal = await db.get(Goal, goal_id)
            if source_goal is None:
                continue
            memory_ids, task_ids, artifact_ids = set(), set(), set()
            for kind, target in (("memory", memory_ids), ("task", task_ids), ("output_artifact", artifact_ids)):
                for record_id, record_root in list(selected[kind].items())[:128]:
                    if record_root == root and await fresh_work_source_scope(operator, task.task_id, kind, record_id, db=db) == root:
                        target.add(record_id)
            historical_owner = WorkBoardOwner(principal_id=source_goal.owner_principal_id, session_id=root)
            historical_task = WorkBoardTask(task_id=task.task_id, goal_id=goal_id,
                goal_revision=source_goal.revision, owner_principal_id=historical_owner.principal_id,
                owner_session_id=root, idempotency_key="local-evidence-read")
            records = await _memory_sources(db, historical_owner, historical_task, selected_ids=memory_ids)
            outputs, source_blocked = await _artifact_sources(db, historical_owner, historical_task,
                selected_task_ids=task_ids, selected_artifact_ids=artifact_ids)
            for source in [*records, *outputs]:
                source["model_context_allowed"] = False
                source["ownership_access"] = "recovered_read_only"
            historical.extend([*records, *outputs])
            blocked.extend(source_blocked)
        sources.extend(historical)
    return sources[:128], blocked


async def _render(db, owner, task, packet, operator=None):
    if packet is None:
        return {"revision": 0, "digest": None, "claims": [], "excluded_source_ids": [],
                "mode": "lexical_degraded", "reason": "remote_embeddings_not_used",
                "invalidated_count": 0, "blocked_sources": [], "memory_status": "no_learning",
                "allow_model_context": False}
    sources, blocked = await _sources(db, owner, task, operator=operator)
    by_id = {s["source_id"]: s for s in sources}
    claims = []
    for citation in packet["citations"]:
        source = by_id.get(citation["source_id"])
        if source is None or source["source_digest"] != citation["source_digest"] or source["version"] != citation["version"]:
            continue
        lines = source["text"].splitlines()
        start, end = citation["line_start"], citation["line_end"]
        text = "\n".join(lines[start - 1:end])
        if _digest(text.encode()) != citation["span_digest"]:
            continue
        age_days = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(source["updated_at"])).days)
        claims.append({**citation, "text": text, "source_kind": source["source_kind"],
            "owner_session_id": source["owner_session_id"], "owner_principal_id": source["owner_principal_id"],
            "confidence": source["confidence"], "freshness": "recent" if age_days <= 7 else "stale",
            "updated_at": source["updated_at"], "memory_id": source["memory_id"],
            "model_context_allowed": source["model_context_allowed"], "private_source": source["private_source"], "page": None, "row": None})
        claims[-1]["ownership_access"] = source.get("ownership_access", "current")
    return {"revision": packet["revision"], "digest": packet["digest"], "task_id": task.task_id,
            "goal_id": task.goal_id, "query": packet["query"], "claims": claims,
            "excluded_source_ids": packet["excluded_source_ids"],
            "allow_model_context": bool(claims) and len(claims) == len(packet["citations"]) and await _is_adopted(db, task, packet),
            "invalidated_count": len(packet["citations"]) - len(claims), "blocked_sources": blocked,
            "mode": "lexical_degraded", "reason": "remote_embeddings_not_used", "memory_status": "no_learning"}


async def read_evidence(db, owner: WorkBoardOwner, task_id: str, operator=None):
    task, read_owner = await _read_task(db, owner, task_id, operator=operator)
    packet = await _render(db, read_owner, task, await _latest(db, task), operator=operator)
    if read_owner.session_id != owner.session_id:
        packet.update(ownership_access="recovered_read_only", execution_block_reason="current_scope_review_required")
    return packet


async def refresh_evidence(db, owner: WorkBoardOwner, task_id: str, request: EvidenceRequest, operator=None):
    await _begin_sqlite_immediate(db)
    task = await _task(db, owner, task_id)
    if task.task_revision != request.expected_task_revision:
        raise BoardError("stale_revision", "Reload the task before refreshing evidence", status_code=409)
    if task.status in {WorkBoardStatus.running, WorkBoardStatus.archived}:
        raise BoardError("evidence_task_locked", "Evidence cannot change while the task is running or archived", status_code=409)
    latest = await _latest(db, task)
    revision = latest["revision"] if latest else 0
    if revision != request.expected_packet_revision:
        raise BoardError("evidence_revision_stale", "Reload the evidence packet", status_code=409)
    exclusions = latest["excluded_source_ids"] if latest else []
    if isinstance(request, EvidenceExclusionRequest):
        if any(not _TOKEN.fullmatch(value) for value in request.excluded_source_ids):
            raise BoardError("evidence_source_invalid", "Source IDs must be opaque packet IDs", status_code=422)
        known = {c["source_id"] for c in (latest or {}).get("citations", [])} | set(exclusions)
        if not set(request.excluded_source_ids) <= known:
            raise BoardError("evidence_source_invalid", "The source is not in this packet", status_code=422)
        exclusions = list(dict.fromkeys([*exclusions, *request.excluded_source_ids]))[:MAX_SOURCES]
    query = request.query if request.query is not None else (latest["query"] if latest else task.title[:200])
    query = await WorkBoardRepository._safe_text(query, db=db)
    if query == "[redaction unavailable]":
        raise BoardError("evidence_redaction_unavailable", "Restore vault redaction before retrieving evidence", status_code=409)
    query = query[:200]
    sources, _blocked = await _sources(db, owner, task, operator=operator)
    terms = _query_terms(query)
    candidates = []
    for source in sources:
        if source["source_id"] in exclusions:
            continue
        for line_number, line in enumerate(source["text"].splitlines(), 1):
            if not line.strip() or len(line) > 1000:
                continue
            score = _term_overlap_score(line, terms)
            if terms and score == 0:
                continue
            candidates.append((score + _recency_boost(datetime.fromisoformat(source["updated_at"]), now=datetime.now(timezone.utc)),
                {"source_id": source["source_id"], "source_digest": source["source_digest"],
                 "version": source["version"], "line_start": line_number, "line_end": line_number,
                 "span_digest": _digest(line.encode())}))
    candidates.sort(key=lambda item: (-item[0], item[1]["source_id"], item[1]["line_start"]))
    packet = {"schema_version": 1, "task_id": task.task_id, "goal_id": task.goal_id,
              "goal_revision": task.goal_revision, "revision": revision + 1, "query": query,
              "excluded_source_ids": exclusions, "allow_model_context": False,
              "citations": [citation for _, citation in candidates[:MAX_CLAIMS]]}
    digest = _write_packet(task, packet)
    db.add(WorkBoardEvent(task_id=task.task_id, owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id, actor_principal_id=owner.principal_id,
        actor_session_id=owner.session_id, kind="task.evidence.updated",
        metadata_json=json.dumps({"packet_revision": revision + 1, "packet_digest": digest,
                                  "excluded_source_ids": exclusions})))
    await db.flush()
    return await _render(db, owner, task, {**packet, "digest": digest}, operator=operator)


async def _is_adopted(db, task, packet):
    event = (await db.execute(select(WorkBoardEvent).where(
        WorkBoardEvent.task_id == task.task_id,
        WorkBoardEvent.owner_principal_id == task.owner_principal_id,
        WorkBoardEvent.owner_session_id == task.owner_session_id,
        WorkBoardEvent.kind.in_(["task.evidence.adopted", "task.evidence.revoked"]),
    ).order_by(WorkBoardEvent.event_id.desc()).limit(1))).scalar_one_or_none()
    metadata = _load(event.metadata_json, {}) if event else {}
    return bool(event and event.kind == "task.evidence.adopted"
        and metadata.get("packet_revision") == packet["revision"]
        and metadata.get("packet_digest") == packet["digest"]
        and metadata.get("task_revision") == task.task_revision)


async def adopt_evidence(db, owner, task_id, request: EvidenceAdoptionRequest, operator=None):
    """Adopt only the exact already-rendered packet, without retrieving new spans."""
    await _begin_sqlite_immediate(db)
    task = await _task(db, owner, task_id)
    if task.task_revision != request.expected_task_revision:
        raise BoardError("stale_revision", "Reload the task before adopting evidence", status_code=409)
    if task.status in {WorkBoardStatus.running, WorkBoardStatus.archived}:
        raise BoardError("evidence_task_locked", "Evidence is locked for this task", status_code=409)
    packet = await _latest(db, task)
    if (packet is None or packet["revision"] != request.expected_packet_revision
        or packet["digest"] != request.expected_packet_digest):
        raise BoardError("evidence_revision_stale", "Review the current packet before adoption", status_code=409)
    rendered = await _render(db, owner, task, packet, operator=operator)
    if request.allow_model_context and (rendered["invalidated_count"] or not rendered["claims"]):
        raise BoardError("evidence_source_changed", "Refresh and review the sources before adoption", status_code=409)
    if rendered["allow_model_context"] == request.allow_model_context:
        raise BoardError("evidence_adoption_stale", "Reload the packet adoption state", status_code=409)
    db.add(WorkBoardEvent(task_id=task.task_id, owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id, actor_principal_id=owner.principal_id,
        actor_session_id=owner.session_id,
        kind="task.evidence.adopted" if request.allow_model_context else "task.evidence.revoked",
        metadata_json=json.dumps({"packet_revision": packet["revision"], "packet_digest": packet["digest"],
                                  "task_revision": task.task_revision})))
    await db.flush()
    return await _render(db, owner, task, packet, operator=operator)


async def evidence_for_task_context(db, owner: WorkBoardOwner, task_id: str, job_id: str, operator=None):
    await _task(db, owner, task_id)  # Read recovery never authenticates execution.
    packet = await read_evidence(db, owner, task_id, operator=operator)
    if not packet["revision"]:
        return None
    if packet["invalidated_count"]:
        raise BoardError("evidence_source_changed", "Refresh the task evidence before model use", status_code=409)
    if not packet["allow_model_context"]:
        raise BoardError("evidence_model_context_consent_required", "Review and explicitly allow this packet for task model context", status_code=403)
    if any(not claim["model_context_allowed"] for claim in packet["claims"]):
        raise BoardError("evidence_source_purpose_consent_required", "Private Mail/Calendar evidence is readable locally; exclude it before strategist model use or review source-purpose consent", status_code=403)
    return {"revision": packet["revision"], "digest": packet["digest"], "mode": packet["mode"],
            "claims": [{"text": c["text"], "source_id": c["source_id"], "source_digest": c["source_digest"],
                        "line_start": c["line_start"], "line_end": c["line_end"], "confidence": c["confidence"]}
                       for c in packet["claims"]]}


async def verify_evidence_use(db, owner: WorkBoardOwner, task_id: str, job_id: str,
                              prepared: dict[str, Any] | None, operator=None, record_use=True):
    """Revalidate the exact prompt binding inside the provider-contact CAS."""
    task = await _task(db, owner, task_id)
    latest = await _latest(db, task)
    if prepared is not None and (latest is None or latest["revision"] != prepared.get("revision")
        or latest["digest"] != prepared.get("digest")):
        raise BoardError("evidence_revision_stale", "The evidence changed before task model contact; refresh and review", status_code=409)
    current = await evidence_for_task_context(db, owner, task_id, job_id, operator=operator)
    if current != prepared:
        raise BoardError("evidence_revision_stale", "The evidence changed before task model contact; refresh and review", status_code=409)
    if record_use:
        await record_evidence_use(db, owner, task_id, job_id, current)


async def record_evidence_use(db, owner: WorkBoardOwner, task_id: str, job_id: str, current):
    """Neutral receipt written only by the winner of the contact transaction."""
    if current is not None:
        db.add(WorkBoardEvent(task_id=task_id, owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, actor_principal_id=owner.principal_id,
            actor_session_id=owner.session_id, kind="task.evidence.used",
            metadata_json=json.dumps({"packet_revision": current["revision"], "packet_digest": current["digest"], "job_id": job_id})))
        await db.flush()
