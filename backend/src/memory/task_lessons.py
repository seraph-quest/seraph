"""Private, evidence-bound task lessons. Candidates are data, never authority."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
from typing import Annotated, Literal
import re
from uuid import UUID
from dataclasses import dataclass
import asyncio
import os
import sys
import ctypes
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, TypeAdapter
from sqlalchemy import select, exists
from sqlalchemy.orm import aliased

from config.settings import settings
from src.db import engine as db_engine
from src.db.models import Goal, MemoryProposal, MemoryProposalStatus, WorkBoardAttempt, WorkBoardTask, WorkflowRunState, WorkflowStepState, WorkBoardEvent, OperatorSession
from src.memory.procedure_recommendations import assert_current_root, canonical, digest, read_private_proof
from src.work_board.repository import BoardError, _begin_sqlite_immediate
from src.workflows.procedure_contracts import ProcedureCandidateV3, ProcedureSaveRequest

PROPOSAL_SCHEMA = "task_method_proposal.v1"
_STAGE_SEAL = object()
_AUTOMATIC_IO = {}
_AUTOMATIC_CALLBACKS = {}
_IO_STARTED = "task_lesson.automatic_io.started.v1"
_IO_FINISHED = "task_lesson.automatic_io.finished.v1"
_IO_CANCELLED = "task_lesson.automatic_io.cancelled.v1"
_MAX_LESSON_BYTES = 64 * 1024
_PROCESS_INSTANCE = str(uuid4())


def _host_platform():
    return sys.platform


class _DarwinBsdInfo(ctypes.Structure):
    # Apple public proc_info.h, MAXCOMLEN=16; fixed supported ABI only.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
    _fields_ += [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


def _darwin_boot_id():
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    query = library.sysctlbyname
    query.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    query.restype = ctypes.c_int
    buffer, size = ctypes.create_string_buffer(37), ctypes.c_size_t(37)
    if query(b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0) != 0 or size.value != 37:
        raise OSError("native boot-session witness unavailable")
    return str(UUID(buffer.value.decode("ascii")))


def _darwin_start(pid):
    if (sys.byteorder != "little" or ctypes.sizeof(_DarwinBsdInfo) != 136
            or _DarwinBsdInfo.pid.offset != 12 or _DarwinBsdInfo.start_sec.offset != 120
            or _DarwinBsdInfo.start_usec.offset != 128 or not 0 < pid <= 2147483647):
        raise OSError("native process witness ABI unsupported")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    query = library.proc_pidinfo
    query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    query.restype = ctypes.c_int
    info = _DarwinBsdInfo()
    if query(pid, 3, 0, ctypes.byref(info), 136) != 136 or info.pid != pid or not info.start_sec or info.start_usec >= 1000000:
        raise OSError("native process lifetime witness unknown")
    return info.start_sec, info.start_usec


def _process_identity(pid=None):
    from pathlib import Path
    pid = os.getpid() if pid is None else pid
    try:
        if _host_platform() == "linux":
            return LinuxProcessWitness(pid=pid, start=Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
                boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip()).model_dump(mode="json")
        if _host_platform() == "darwin":
            boot_id = _darwin_boot_id()
            seconds, microseconds = _darwin_start(pid)
            return DarwinProcessWitness(pid=pid, boot_id=boot_id,
                start_sec=seconds, start_usec=microseconds).model_dump(mode="json")
    except (OSError, ValueError, IndexError):
        pass
    return UnknownProcessWitness(pid=pid, platform=_host_platform(), instance_id=_PROCESS_INSTANCE).model_dump(mode="json")


def _process_ended(identity):
    from pathlib import Path
    try:
        if set(identity) == {"pid", "start", "boot_id"}:
            identity = LinuxProcessWitness(**identity).model_dump(mode="json")
        witness = PROCESS_WITNESS.validate_python(identity)
        if isinstance(witness, LinuxProcessWitness) and _host_platform() == "linux":
            if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != witness.boot_id:
                return True
            try:
                actual = Path(f"/proc/{witness.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            except FileNotFoundError:
                return True
            return actual != witness.start
        if isinstance(witness, DarwinProcessWitness) and _host_platform() == "darwin":
            if _darwin_boot_id() != witness.boot_id:
                return True
            # Lookup failure/PID absence is unknown, never termination proof.
            return _darwin_start(witness.pid) != (witness.start_sec, witness.start_usec)
        return False
    except (OSError, ValueError, IndexError):
        return False


async def _io_event(db, start, kind):
    previous = (await db.execute(select(WorkBoardEvent).where(
        WorkBoardEvent.kind == kind, WorkBoardEvent.mutation_request_digest == start.mutation_request_digest))).scalar_one_or_none()
    if previous is None:
        db.add(WorkBoardEvent(task_id=start.task_id, owner_principal_id=start.owner_principal_id,
            owner_session_id=start.owner_session_id, actor_principal_id="service:task-lesson-status",
            actor_session_id=None, kind=kind, mutation_request_digest=start.mutation_request_digest,
            metadata_json=start.metadata_json))


async def _finish_io(start):
    # This is status provenance only: expiry/revocation cannot mint proposal authority.
    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        await _io_event(db, start, _IO_FINISHED)
    _AUTOMATIC_IO.pop(start.mutation_request_digest, None)


async def _recover_ended_io():
    """Recover an original terminated process; missing registry is not proof."""
    finished = aliased(WorkBoardEvent)
    async with db_engine.get_session() as db:
        pending = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind == _IO_STARTED,
            ~exists(select(finished.event_id).where(finished.kind == _IO_FINISHED,
                finished.mutation_request_digest == WorkBoardEvent.mutation_request_digest))).limit(1))).scalar_one_or_none()
        if pending is None:
            return
        retained = _AUTOMATIC_IO.get(pending.mutation_request_digest)
        task = await _task(db, pending.task_id)
        metadata = json.loads(pending.metadata_json)
    if retained is not None:
        if retained.done() and not retained.cancelled():
            await _finish_io(pending)
        return
    if not await asyncio.to_thread(_process_ended, metadata["process"]):
        return
    # Positive original-process termination prevents a surviving staging writer.
    # An immutable staged artifact remains private; it is never auto-adopted.
    try:
        await asyncio.to_thread(read_private_proof, metadata["artifact_ref"], metadata["candidate_digest"])
        reason = "automatic_lesson_recovered_private_stage_no_change"
    except (OSError, ValueError, BoardError):
        reason = "automatic_lesson_recovered_stage_unavailable_no_change"
    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        await _io_event(db, pending, _IO_CANCELLED)
        await _io_event(db, pending, _IO_FINISHED)
    original = task.model_copy(update={"task_revision": metadata["task_revision"]})
    await _record_automatic_outcome(None, original, {"status": "blocked", "result": "no_change",
        "reason_code": reason, "behavior_changed": False}, metadata["attempt_id"], status_only=True)


async def _reserve_io(operator, request, token, binding, relative, sha, policy, staged):
    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        task, _, _, current = await _source(db, operator, request, automatic=True, staged=staged)
        current["method_receipt"] = staged.method_token
        if current != token or await _automatic_policy(db, operator, task) != policy:
            raise BoardError("lesson_source_changed", "Refresh the exact source and automatic policy")
        original = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind == _IO_STARTED,
            WorkBoardEvent.mutation_request_digest == binding))).scalar_one_or_none()
        if original is not None:
            return original, False
        finished = aliased(WorkBoardEvent)
        pending = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind == _IO_STARTED,
            ~exists(select(finished.event_id).where(finished.kind == _IO_FINISHED,
                finished.mutation_request_digest == WorkBoardEvent.mutation_request_digest))).limit(1))).scalar_one_or_none()
        if pending is not None:
            raise BoardError("automatic_lesson_io_pending", "The original private staging operation retains capacity")
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        starts = list((await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind == _IO_STARTED,
            WorkBoardEvent.owner_principal_id == task.owner_principal_id, WorkBoardEvent.created_at >= midnight))).scalars())
        legacy = list((await db.execute(select(MemoryProposal).where(MemoryProposal.schema_version == PROPOSAL_SCHEMA,
            MemoryProposal.owner_principal_id == task.owner_principal_id, MemoryProposal.created_at >= midnight))).scalars())
        counted = {event.mutation_request_digest for event in starts}
        counted.update(row.request_binding_digest for row in legacy if json.loads(row.provenance_json).get("automatic"))
        if len(counted) >= 2:
            raise BoardError("automatic_lesson_daily_cap", "The owner daily automatic proposal-start cap is reached")
        start = WorkBoardEvent(task_id=task.task_id, owner_principal_id=task.owner_principal_id,
            owner_session_id=task.owner_session_id, actor_principal_id=operator.principal.principal_id,
            actor_session_id=operator.session_id, kind=_IO_STARTED, mutation_request_digest=binding,
            metadata_json=canonical({"task_revision": task.task_revision, "attempt_id": request.attempt_id,
                "source_digest": digest(token), "artifact_ref": relative, "candidate_digest": sha,
                "policy_revision": policy["policy_revision"], "process": _process_identity()}))
        db.add(start)
        await db.commit()
        await db.refresh(start)
        return start, True


def _write_lesson(relative, raw, sha):
    from src.workspace import canonical_workspace_root
    from src.work_board.input_artifacts import _write_payload
    if len(raw) > _MAX_LESSON_BYTES:
        raise ValueError("task lesson artifact exceeds byte limit")
    _write_payload(canonical_workspace_root(settings.workspace_dir) / relative, raw)
    return read_private_proof(relative, sha)


@dataclass(frozen=True)
class _SourceStage:
    seal: object
    token: dict
    method: TaskMethod | ProcedureCandidateV3 | None
    method_token: dict | None
    procedure_source: object | None = None
BoundedText = Annotated[str, Field(min_length=1, max_length=1000)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]


async def _task(db, task_id):
    # Public task_id is unique; the SQLite ordering sequence is the primary key.
    return (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id)
        .execution_options(populate_existing=True))).scalar_one_or_none()


async def _assert_owner(db, operator, *, automatic=False):
    if not automatic:
        return await assert_current_root(db, operator)
    # A canonical opt-in event authorizes the terminal owner callback, not a
    # new bearer token, renewed Root, service principal or trace-egress grant.
    row = await db.get(OperatorSession, operator.session_id, populate_existing=True)
    utc = lambda value: value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if (row is None or row.principal_id != operator.principal.principal_id
        or not operator.principal.authenticated or operator.ownership_continuity != "stable"
        or getattr(operator.principal.principal_type, "value", operator.principal.principal_type) != "operator"
        or row.revoked_at is not None or row.replaced_by_id is not None or row.is_bearer_tombstone
        or utc(row.idle_expires_at) <= now or utc(row.absolute_expires_at) <= now):
        raise BoardError("lesson_root_stale", "The original automatic-proposal owner is no longer current")
    return row


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LinuxProcessWitness(ClosedModel):
    schema_version: Literal["lesson_process_witness.v1"] = "lesson_process_witness.v1"
    kind: Literal["linux"] = "linux"
    pid: int = Field(ge=1)
    start: str = Field(pattern=r"^[0-9]+$", max_length=32)
    boot_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


class DarwinProcessWitness(ClosedModel):
    schema_version: Literal["lesson_process_witness.v1"] = "lesson_process_witness.v1"
    kind: Literal["darwin"] = "darwin"
    pid: int = Field(ge=1)
    start_sec: int = Field(ge=1)
    start_usec: int = Field(ge=0, lt=1000000)
    boot_id: str = Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


class UnknownProcessWitness(ClosedModel):
    schema_version: Literal["lesson_process_witness.v1"] = "lesson_process_witness.v1"
    kind: Literal["unknown"] = "unknown"
    platform: str = Field(min_length=1, max_length=32)
    pid: int = Field(ge=1)
    instance_id: str = Field(pattern=r"^[a-f0-9-]{36}$")


PROCESS_WITNESS = TypeAdapter(Annotated[LinuxProcessWitness | DarwinProcessWitness | UnknownProcessWitness, Field(discriminator="kind")])


class ResearchStrategy(ClosedModel):
    schema_version: Literal["ResearchStrategy.v1"] = "ResearchStrategy.v1"
    query_templates: list[BoundedText] = Field(max_length=3)
    source_preferences: list[Literal["primary", "official", "peer_reviewed", "dated", "independent"]] = Field(max_length=5)
    required_evidence_fields: list[Literal["url", "title", "date", "excerpt", "claim", "limitation"]] = Field(max_length=6)
    draft_sections: list[BoundedText] = Field(max_length=16)
    stop_conditions: list[BoundedText] = Field(min_length=1, max_length=8)


class MethodOutput(ClosedModel):
    artifact_type: Identifier
    required_fields: list[Identifier] = Field(min_length=1, max_length=16)


class ToolStep(ClosedModel):
    kind: Literal["registered_tool"] = "registered_tool"
    tool_id: Identifier


class GuardStep(ClosedModel):
    kind: Literal["guard"] = "guard"
    check: Literal["source_exists", "verified_readback", "preserve_source_attribution"]


class CapabilityStep(ClosedModel):
    """Snapshot of one current native interface; never a generic invoke string."""
    kind: Literal["registered_capability"] = "registered_capability"
    capability_id: Literal["work.json-format.v1"]
    capability_version: Literal["1"]
    typed_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


MethodStep = Annotated[ToolStep | GuardStep | CapabilityStep, Field(discriminator="kind")]


class TaskMethod(ClosedModel):
    schema_version: Literal["TaskMethod.v1"] = "TaskMethod.v1"
    family: Literal["research", "software", "knowledge", "general"]
    steps: list[MethodStep] = Field(min_length=1, max_length=16)
    registered_tool_ids: list[Identifier] = Field(max_length=16)
    input_parameters: dict[Identifier, str | int | bool | None] = Field(max_length=16)
    output_contract: MethodOutput

    @field_validator("registered_tool_ids")
    @classmethod
    def registered(cls, values):
        from src.native_tools.registry import TOOL_METADATA
        if len(values) != len(set(values)) or any(value not in TOOL_METADATA for value in values):
            raise ValueError("Only existing registered tools may be referenced")
        return values

    @field_validator("input_parameters")
    @classmethod
    def parameters(cls, values):
        forbidden = {"code", "command", "script", "permissions", "provider", "model", "runtime_limits", "credentials", "secret_ref", "api_key", "install"}
        if any(key.lower() in forbidden or (isinstance(value, str) and len(value) > 1000) for key, value in values.items()):
            raise ValueError("Method input cannot change execution, permissions, providers or credentials")
        return values

    @field_validator("steps")
    @classmethod
    def valid_steps(cls, values):
        from src.native_tools.registry import TOOL_METADATA
        if any(isinstance(step, ToolStep) and step.tool_id not in TOOL_METADATA for step in values):
            raise ValueError("Each tool step must reference an existing registered tool")
        return values


Candidate = Annotated[ResearchStrategy | TaskMethod | ProcedureCandidateV3, Field(discriminator="schema_version")]


class LessonScope(ClosedModel):
    goal_id: Identifier
    goal_revision: int = Field(ge=1)
    family: Literal["research", "software", "knowledge", "general"]


class LessonRequest(ClosedModel):
    task_id: Identifier
    attempt_id: Identifier
    correction: str = Field(max_length=4000)
    source_refs: list[Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")]] = Field(min_length=1, max_length=16)
    scope: LessonScope
    expected_revision: int = Field(ge=1)

    @field_validator("source_refs")
    @classmethod
    def unique_refs(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Source references must be unique")
        return values


class ResearchMethodRequest(ClosedModel):
    task_id: Identifier
    attempt_id: Identifier
    source_refs: list[Annotated[str, Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")]] = Field(min_length=1, max_length=16)
    scope: LessonScope
    expected_revision: int = Field(ge=1)
    strategy: ResearchStrategy

    @field_validator("scope")
    @classmethod
    def research_scope(cls, value):
        if value.family != "research":
            raise ValueError("Structured research methods require the research family")
        return value


class LessonAutoPolicyRequest(ClosedModel):
    enabled: bool
    expected_revision: int = Field(ge=1)
    expected_policy_revision: int | None = Field(..., ge=1)
    mutation_uuid: str

    @field_validator("mutation_uuid")
    @classmethod
    def canonical_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("mutation_uuid must be a canonical UUID")
        return value


def _policy_binding(task):
    return digest({"task_id": task.task_id, "goal_id": task.goal_id, "goal_revision": task.goal_revision,
        "capability_id": task.capability_id, "typed_input_digest": task.typed_input_digest})


async def _automatic_policy(db, operator, task):
    event = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.task_id == task.task_id,
        WorkBoardEvent.owner_principal_id == operator.principal.principal_id,
        WorkBoardEvent.owner_session_id == operator.session_id,
        WorkBoardEvent.kind == "task_lesson.automatic_policy.v1")
        .order_by(WorkBoardEvent.event_id.desc()).limit(1))).scalar_one_or_none()
    state = json.loads(event.metadata_json) if event else {}
    return {"enabled": state.get("enabled") is True and state.get("task_binding") == _policy_binding(task),
        "policy_revision": event.event_id if event else None, "daily_cap": 2,
        "inference_egress": "not_permitted", "adoption": "requires_separate_review"}


async def set_automatic_lesson_policy(operator, task_id, request: LessonAutoPolicyRequest):
    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        await assert_current_root(db, operator)
        task = await _task(db, task_id)
        owner = (operator.principal.principal_id, operator.session_id)
        if task is None or (task.owner_principal_id, task.owner_session_id) != owner:
            raise BoardError("lesson_owner_mismatch", "The task belongs to another operator", status_code=403)
        if task.task_revision != request.expected_revision:
            raise BoardError("lesson_task_changed", "Refresh the current task before changing automatic proposal consent")
        request_sha = digest({"task_id": task_id, **request.model_dump()})
        existing = (await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.owner_principal_id == owner[0], WorkBoardEvent.owner_session_id == owner[1],
            WorkBoardEvent.mutation_idempotency_key == request.mutation_uuid))).scalar_one_or_none()
        if existing:
            if existing.kind != "task_lesson.automatic_policy.v1" or existing.mutation_request_digest != request_sha:
                raise BoardError("lesson_policy_request_conflict", "This request key already binds another change")
            return await _automatic_policy(db, operator, task)
        policy = await _automatic_policy(db, operator, task)
        if request.expected_policy_revision != policy["policy_revision"]:
            raise BoardError("lesson_policy_changed", "Refresh the authoritative automatic policy before changing consent")
        db.add(WorkBoardEvent(task_id=task_id, owner_principal_id=owner[0], owner_session_id=owner[1],
            actor_principal_id=owner[0], actor_session_id=owner[1], kind="task_lesson.automatic_policy.v1",
            mutation_idempotency_key=request.mutation_uuid, mutation_request_digest=request_sha,
            metadata_json=canonical({"enabled": request.enabled, "task_revision": task.task_revision,
                "task_binding": _policy_binding(task), "egress_permitted": False, "daily_cap": 2})))
        await db.flush()
        return await _automatic_policy(db, operator, task)


async def _source(db, operator, request: LessonRequest, *, automatic=False, staged=None):
    """Read current authority and the actual durable attempt, never a caller vote."""
    await _assert_owner(db, operator, automatic=automatic)
    task = await _task(db, request.task_id)
    owner = (operator.principal.principal_id, operator.session_id)
    if task is None or (task.owner_principal_id, task.owner_session_id) != owner:
        raise BoardError("lesson_owner_mismatch", "The task belongs to another operator", status_code=403)
    if task.task_revision != request.expected_revision:
        raise BoardError("lesson_task_changed", "Refresh the exact task revision")
    goal = await db.get(Goal, task.goal_id, populate_existing=True)
    if (goal is None or (goal.owner_principal_id, goal.owner_session_id) != owner
        or (task.goal_id, task.goal_revision) != (request.scope.goal_id, request.scope.goal_revision)
        or goal.revision != request.scope.goal_revision):
        raise BoardError("lesson_scope_changed", "The current goal and scope must match")
    attempt = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id)
        .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))).scalar_one_or_none()
    if attempt is None or attempt.attempt_id != request.attempt_id or attempt.ended_at is None:
        raise BoardError("lesson_attempt_unverified", "The current attempt must have a terminal receipt")
    run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).scalar_one_or_none()
    from src.work_board.review import _workflow_run_binds_board_attempt
    if run is None or (staged is None and not _workflow_run_binds_board_attempt(task, attempt, run)):
        raise BoardError("lesson_run_unverified", "The durable run must bind the exact attempt")
    db_binding = digest([[{column.name: str(getattr(row, column.name)) for column in row.__table__.columns}]
        for row in (task, attempt, run, goal)])
    if staged is not None:
        if not isinstance(staged, _SourceStage) or staged.seal is not _STAGE_SEAL or staged.token.get("db_binding_digest") != db_binding:
            raise BoardError("lesson_source_changed", "The staged canonical source changed")
    authority = json.loads(run.declared_authority_json or "{}")
    # Existing native no_learning means that invocation performs no learning.
    # It does not exclude a later explicit metadata-only lesson. Distinct
    # source-specific exclusions remain absolute; source bodies are not copied
    # into lessons or projected to any provider by verification.
    if authority.get("source_learning_excluded") is True:
        raise BoardError("lesson_source_excluded", "This source explicitly excludes learning")
    effects = json.loads(run.effect_receipts_json)
    if any(isinstance(effect, dict) and effect.get("status") in {"unknown", "contact_started", "pending", "intent", "dispatched"} for effect in effects):
        raise BoardError("lesson_outcome_unresolved", "Unresolved contacted work cannot authorize a lesson")
    if staged is not None:
        observed = staged.token["observed"]
        verified_refs = staged.token["verified_refs"]
    elif run.status == "succeeded":
        from src.memory.m5 import _verified_source, _source_refs
        try:
            proof = await _verified_source(db, task, requested_attempt_id=attempt.attempt_id)
        except ValueError as exc:
            raise BoardError("lesson_source_unverified", "The ordinary task readback is not verified") from exc
        verified_refs = _source_refs(proof.readback)
        observed = {"status": "completed", "readback_digest": proof.evidence_digest}
    elif run.status == "failed" and run.finished_at is not None:
        # Failure is an observation, not a successful readback or positive vote.
        verified_refs = [run.run_identity, attempt.attempt_id]
        observed = {"status": "failed", "failure_reason_digest": digest(run.failure_reason or run.error or "unspecified")}
    else:
        raise BoardError("lesson_outcome_unresolved", "Unresolved, blocked or contacted-unknown work cannot authorize reflection")
    if not set(request.source_refs).issubset(verified_refs):
        raise BoardError("lesson_source_unbound", "Only references verified against this exact attempt are allowed")
    token = {"task_revision": task.task_revision, "goal_revision": goal.revision,
        "attempt_id": attempt.attempt_id, "fence": attempt.fencing_token,
        "run_id": run.run_identity, "run_revision": run.revision,
        "receipt_digest": digest(attempt.receipt_refs_json), "artifacts_digest": digest(run.artifact_receipts_json),
        "effects_digest": digest(run.effect_receipts_json), "observed": observed,
        "run_fingerprint": run.run_fingerprint, "input_digest": run.input_digest,
        "authority_digest": digest(run.declared_authority_json), "status": run.status,
        "task_status": task.status.value, "attempt_outcome": attempt.outcome,
        "db_binding_digest": db_binding, "verified_refs": verified_refs,
        "task_intent_digest": digest({"capability": task.capability_id, "input": task.typed_input_digest,
            "goal": task.goal_id, "goal_revision": task.goal_revision})}
    return task, attempt, run, token


async def _observed_method(db, task, run, family, *, structured_research=False):
    """Project only recorded tool identities, never source bodies or arguments."""
    if task.capability_id == "agent.task.v1":
        from src.memory.task_lesson_native import project_completed_native_method
        return await project_completed_native_method(db, task, run, family)
    if structured_research and task.capability_id == "work.research-dossier.v1":
        if family != "research" or run.capability_version != "1" or run.status != "succeeded":
            return None, None
        return None, {"research_capability": "work.research-dossier.v1", "capability_version": "1",
            "typed_input_digest": task.typed_input_digest, "run_input_digest": run.input_digest,
            "artifact_receipts_digest": digest(run.artifact_receipts_json),
            "effect_receipts_digest": digest(run.effect_receipts_json)}
    if task.capability_id == "work.json-format.v1":
        from src.work_board.dispatcher import REGISTERED_CAPABILITIES
        from src.work_board.tool_package_contracts import JsonFormatInput
        from src.execution.tool_package_profile import source_package, MAX_INPUT, MAX_OUTPUT, PROFILE
        capability = REGISTERED_CAPABILITIES.get(task.capability_id)
        if capability is None or capability.version != "1" or run.capability_version != "1":
            return None, None
        authority = json.loads(run.declared_authority_json)
        input_schema = JsonFormatInput.model_json_schema()
        output_contract = {"artifact_type": "tool_package_json", "content": "bounded_sorted_json",
            "max_input_bytes": MAX_INPUT, "max_output_bytes": MAX_OUTPUT, "profile": PROFILE,
            "duplicate_keys": "rejected", "nonfinite_numbers": "rejected"}
        snapshot = {"capability_id": capability.capability_id, "capability_version": capability.version,
            "input_schema": input_schema, "output_contract": output_contract,
            "package_code_digest": hashlib.sha256(source_package().read_bytes()).hexdigest(),
            "admitted_pack": authority.get("pack"), "admitted_runtime": authority.get("runtime")}
        step = CapabilityStep(capability_id=task.capability_id, capability_version=capability.version,
            typed_input_digest=task.typed_input_digest, input_schema_digest=digest(input_schema),
            output_contract_digest=digest(output_contract), contract_snapshot_digest=digest(snapshot))
        method = TaskMethod(family=family, steps=[step], registered_tool_ids=[], input_parameters={},
            output_contract=MethodOutput(artifact_type="tool_package_json", required_fields=["artifact_ref", "content_sha256", "readback_id"]))
        return method, {"native_contract_snapshot": snapshot, "contract_snapshot_digest": digest(snapshot)}
    rows = list((await db.execute(select(WorkflowStepState).where(WorkflowStepState.run_identity == run.run_identity)
        .order_by(WorkflowStepState.step_index.asc()).limit(17))).scalars().all())
    if not 1 <= len(rows) <= 15:
        return None, None
    from src.native_tools.registry import TOOL_METADATA
    if any(row.tool_name not in TOOL_METADATA or row.completed_at is None
        or row.status not in {"succeeded", "completed", "failed", "continued_error"} for row in rows):
        return None, None
    steps = [{"id": row.id, "step_id": row.step_id, "index": row.step_index,
        "tool": row.tool_name, "status": row.status, "updated_at": row.updated_at.isoformat()} for row in rows]
    method = TaskMethod(family=family,
        steps=[ToolStep(tool_id=step["tool"]) for step in steps],
        registered_tool_ids=list(dict.fromkeys(step["tool"] for step in steps)),
        input_parameters={}, output_contract=MethodOutput(artifact_type="task_result", required_fields=["status", "source_refs"]))
    return method, {"step_refs": [row.id for row in rows], "steps_digest": digest(steps)}


async def _observed_method_after_stage(db, task, run, family, staged, *, structured_research=False):
    """Final original-source metadata check; never perform physical I/O."""
    if staged.procedure_source is not None:
        from src.memory.task_lesson_native import recheck_procedure_source
        audit = await recheck_procedure_source(db, staged.procedure_source)
        return staged.method, audit
    if task.capability_id == "agent.task.v1":
        from src.memory.task_lesson_native import native_source_metadata
        _, _, audit = await native_source_metadata(db, task, run)
        return staged.method, audit
    if task.capability_id == "work.json-format.v1":
        return staged.method, staged.method_token
    return await _observed_method(db, task, run, family, structured_research=structured_research)


def _correct_method(old: TaskMethod | None, correction: str):
    """Finite deterministic lesson grammar; arbitrary prose remains evidence only."""
    if old is None or not correction.strip():
        return None
    normalized = correction.lower()
    if re.search(r"\b(check|verify|ensure)\b.*\b(source|file)\b.*\b(exists?|existence|present)\b", normalized):
        guard, before = "source_exists", True
    elif re.search(r"\b(verify|verified|require|check)\b.*\b(readback|read.back)\b", normalized):
        guard, before = "verified_readback", False
    elif re.search(r"\b(preserve|include|keep|require)\b.*\b(attribution|citations?|source references)\b", normalized):
        guard, before = "preserve_source_attribution", False
    else:
        return None
    added = GuardStep(check=guard)
    return TaskMethod.model_validate({**old.model_dump(), "steps":
        [added.model_dump(), *[step.model_dump() for step in old.steps]] if before
        else [*[step.model_dump() for step in old.steps], added.model_dump()]})


async def create_research_method(operator, request: ResearchMethodRequest):
    source = LessonRequest(task_id=request.task_id, attempt_id=request.attempt_id, correction="",
        source_refs=request.source_refs, scope=request.scope, expected_revision=request.expected_revision)
    return await create_task_lesson(operator, source, _structured_strategy=request.strategy)


async def create_task_lesson(operator, request: LessonRequest, *, _automatic: bool = False,
                             _structured_strategy: ResearchStrategy | None = None,
                             _procedure_candidate: ProcedureCandidateV3 | None = None,
                             _procedure_witness=None, _procedure_request: ProcedureSaveRequest | None = None):
    """Draft locally from an explicit correction; never contact any provider."""
    from src.memory.m5 import sanitize_m5_memory_text_async
    # Vault-aware sanitization is staged before the SQLite writer lock.
    try:
        correction = await sanitize_m5_memory_text_async(request.correction) if request.correction.strip() else ""
    except ValueError as exc:
        unavailable = "unavailable" in str(exc)
        raise BoardError("lesson_redaction_unavailable" if unavailable else "lesson_correction_unsafe",
            "Restore redaction before requesting the lesson" if unavailable else "Remove secret or authority-changing text from the correction",
            status_code=503 if unavailable else 422) from exc
    if len(correction) > 1000:
        raise BoardError("lesson_correction_limit", "Use a correction of at most 1000 characters", status_code=422)
    async with db_engine.get_session() as db:
        task, attempt, run, token = await _source(db, operator, request, automatic=_automatic)
        policy = await _automatic_policy(db, operator, task) if _automatic else None
        if _automatic and not policy["enabled"]:
            return {"status": "blocked", "reason_code": "automatic_lessons_not_opted_in", "result": "no_change", "behavior_changed": False}
        if _procedure_candidate is not None:
            from src.memory.task_lesson_native import recheck_procedure_source, build_procedure_candidate
            if (_automatic or _procedure_request is None or request.scope.family != "general"
                or token["observed"]["status"] != "completed"):
                raise BoardError("procedure_source_unsupported", "Use an explicitly selected completed general task")
            audit_token = await recheck_procedure_source(db, _procedure_witness)
            if build_procedure_candidate(_procedure_witness, _procedure_request.parameter_selections) != _procedure_candidate:
                raise BoardError("procedure_candidate_changed", "Use only original producer-offered parameter fields")
            old = _procedure_candidate
            candidate_text = canonical(_procedure_candidate.model_dump(mode="json"))
            if await sanitize_m5_memory_text_async(candidate_text) != candidate_text:
                raise BoardError("procedure_candidate_unsafe", "Remove secrets or unsafe fixed input from the source task", status_code=422)
        else:
            old, audit_token = await _observed_method(db, task, run, request.scope.family,
                structured_research=_structured_strategy is not None)
        if _structured_strategy is not None:
            if (_automatic or task.capability_id != "work.research-dossier.v1" or run.capability_version != "1"
                or token["observed"]["status"] != "completed" or request.scope.family != "research" or audit_token is None):
                raise BoardError("research_method_source_unsupported", "Use the completed native research dossier and its verified source references")
            candidate_text = canonical(_structured_strategy.model_dump(mode="json"))
            if await sanitize_m5_memory_text_async(candidate_text) != candidate_text:
                raise BoardError("research_method_candidate_unsafe", "Remove private secrets from the typed strategy", status_code=422)
        token["method_receipt"] = audit_token
        staged = _SourceStage(_STAGE_SEAL, token, old, audit_token, _procedure_witness)
        binding_data = {"owner": task.owner_principal_id, "root": task.owner_session_id,
            "request": request.model_dump(), "correction_digest": digest(correction), "source": token,
            "automatic_policy": policy}
        if _structured_strategy is not None:
            binding_data["structured_strategy"] = _structured_strategy.model_dump(mode="json")
        procedure_key = None
        if _procedure_request is not None:
            binding_data["procedure_request"] = _procedure_request.model_dump(mode="json")
            binding_data["procedure_candidate"] = _procedure_candidate.model_dump(mode="json")
            procedure_key = "procedure:" + digest([task.owner_principal_id, task.owner_session_id,
                task.task_id, _procedure_request.idempotency_key])
        binding = digest(binding_data)
        if procedure_key is not None:
            prior_key = await db.scalar(select(MemoryProposal).where(MemoryProposal.request_idempotency_key == procedure_key))
            if prior_key is not None and prior_key.request_binding_digest != binding:
                raise BoardError("procedure_save_idempotency_conflict", "The original save key identifies different source or parameters")
        previous = (await db.execute(select(MemoryProposal).where(
            MemoryProposal.schema_version == PROPOSAL_SCHEMA,
            MemoryProposal.owner_principal_id == task.owner_principal_id,
            MemoryProposal.owner_session_id == task.owner_session_id,
            MemoryProposal.request_binding_digest == binding))).scalar_one_or_none()
        if previous:
            mirror = {"status": "not_requested", "reason_code": "automatic_canonical_receipt"}
            if not _automatic:
                mirror = await _repair_lesson_mirror(previous)
            return {**proposal_projection(previous), "idempotent_replay": True, "mirror": mirror}
        candidate = _procedure_candidate if _procedure_candidate is not None else _structured_strategy if _structured_strategy is not None else _correct_method(old, correction)
        reason = ("observed_failure_candidate" if _automatic else "explicit_correction") if candidate else "insufficient_method_evidence" if old is None else "no_explicit_correction" if not correction else "unsupported_correction_no_change"
        envelope = {"schema_version": PROPOSAL_SCHEMA, "old_method": old.model_dump() if old else None,
            "new_method": candidate.model_dump() if candidate else None, "correction": correction,
            "correction_provenance": "observed_failure_rule" if _automatic else "explicit_operator",
            "lesson_provenance": "deterministic_draft_from_observed_failure" if _automatic else "deterministic_draft_from_correction",
            "observed": token["observed"], "source_token": token, "source_refs": request.source_refs,
            "scope": request.scope.model_dump(), "behavior_changed": False, "positive_preference_vote": False,
            "reflection": {"mode": "local_projection", "provider_contacts": 0, "spend_microusd": 0}}
        if _structured_strategy is not None:
            reason = "explicit_structured_research_method"
            envelope["correction_provenance"] = "explicit_operator_structured_data"
            envelope["lesson_provenance"] = "verified_completed_research_strategy"
        if _procedure_candidate is not None:
            reason = "explicit_completed_procedure_method"
            envelope["old_method"] = None
            envelope["correction_provenance"] = "explicit_operator_parameter_selection"
            envelope["lesson_provenance"] = "verified_completed_general_journey"
            envelope["procedure_parameter_selections"] = [item.model_dump(mode="json") for item in _procedure_request.parameter_selections]
        raw = canonical(envelope).encode()
        sha = hashlib.sha256(raw).hexdigest()
        relative = f"artifacts/memory/task-lessons/{binding}.json"
        staged_task_revision = task.task_revision
    if _automatic:
        try:
            io_start, newly_started = await _reserve_io(operator, request, token, binding, relative, sha, policy, staged)
        except BoardError as exc:
            if exc.code in {"automatic_lesson_daily_cap", "automatic_lesson_io_pending"}:
                return {"status": "blocked", "reason_code": exc.code, "result": "no_change", "behavior_changed": False}
            raise
        if not newly_started:
            original_worker = _AUTOMATIC_IO.get(binding)
            if original_worker is not None or binding in _AUTOMATIC_CALLBACKS:
                return {"status": "blocked", "reason_code": "automatic_lesson_io_pending", "result": "no_change", "behavior_changed": False}
            metadata = json.loads(io_start.metadata_json)
            if not await asyncio.to_thread(_process_ended, metadata["process"]):
                # A completed same-process marker is recoverable only with its
                # positive completion event, never by an empty worker registry.
                async with db_engine.get_session() as db:
                    finished = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind == _IO_FINISHED,
                        WorkBoardEvent.mutation_request_digest == binding))).scalar_one_or_none()
                if finished is None:
                    return {"status": "blocked", "reason_code": "automatic_lesson_io_pending", "result": "no_change", "behavior_changed": False}
            try:
                await asyncio.to_thread(read_private_proof, relative, sha)
                recovered_reason = "automatic_lesson_cancelled_staged_artifact"
            except (OSError, ValueError, BoardError):
                recovered_reason = "automatic_lesson_cancelled_artifact_unavailable"
            async with db_engine.get_session() as db:
                await _begin_sqlite_immediate(db)
                await _io_event(db, io_start, _IO_CANCELLED)
                await _io_event(db, io_start, _IO_FINISHED)
            return {"status": "blocked", "reason_code": recovered_reason, "result": "no_change", "behavior_changed": False}
        worker = asyncio.create_task(asyncio.to_thread(_write_lesson, relative, raw, sha))
        _AUTOMATIC_IO[binding] = worker
        _AUTOMATIC_CALLBACKS[binding] = asyncio.current_task()
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Cancellation cannot stop a thread. Retain its single slot until
            # positive completion; the thread has no DB/proposal capability.
            def completed(done):
                if not done.cancelled():
                    done.exception()
                    asyncio.create_task(_finish_io(io_start))
            worker.add_done_callback(completed)
            raise
        finally:
            if worker.done() and not worker.cancelled():
                await _finish_io(io_start)
    else:
        await asyncio.to_thread(_write_lesson, relative, raw, sha)
    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        task, attempt, run, current = await _source(db, operator, request, automatic=_automatic, staged=staged)
        current_method, current_audit = await _observed_method_after_stage(db, task, run, request.scope.family,
            staged, structured_research=_structured_strategy is not None)
        current["method_receipt"] = current_audit
        if current != token or task.task_revision != staged_task_revision:
            raise BoardError("lesson_source_changed", "The exact ordinary task evidence changed; request a new lesson")
        if procedure_key is not None:
            prior_key = await db.scalar(select(MemoryProposal).where(MemoryProposal.request_idempotency_key == procedure_key))
            if prior_key is not None and prior_key.request_binding_digest != binding:
                raise BoardError("procedure_save_idempotency_conflict", "The original save key identifies different source or parameters")
        previous = (await db.execute(select(MemoryProposal).where(
            MemoryProposal.schema_version == PROPOSAL_SCHEMA,
            MemoryProposal.owner_principal_id == task.owner_principal_id,
            MemoryProposal.owner_session_id == task.owner_session_id,
            MemoryProposal.request_binding_digest == binding))).scalar_one_or_none()
        if previous:
            # Release the SQLite writer before touching the independent mirror.
            await db.commit()
            mirror = {"status": "not_requested", "reason_code": "automatic_canonical_receipt"}
            if not _automatic:
                mirror = await _repair_lesson_mirror(previous)
            return {**proposal_projection(previous), "idempotent_replay": True, "mirror": mirror}
        if _automatic:
            current_policy = await _automatic_policy(db, operator, task)
            if current_policy != policy or not current_policy["enabled"]:
                raise BoardError("lesson_policy_changed", "Automatic proposal consent changed during staging")
            authoritative_start = await db.get(WorkBoardEvent, io_start.event_id, populate_existing=True)
            if (authoritative_start is None or authoritative_start.kind != _IO_STARTED
                or authoritative_start.mutation_request_digest != binding
                or authoritative_start.metadata_json != io_start.metadata_json):
                raise BoardError("automatic_lesson_start_changed", "The exact original staging reservation changed")
            cancelled = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind == _IO_CANCELLED,
                WorkBoardEvent.mutation_request_digest == binding))).scalar_one_or_none()
            if cancelled is not None or asyncio.current_task().cancelling():
                raise BoardError("automatic_lesson_cancelled", "The original automatic callback cannot commit after cancellation")
        row = MemoryProposal(schema_version=PROPOSAL_SCHEMA, owner_principal_id=task.owner_principal_id,
            owner_session_id=task.owner_session_id, source_task_id=task.task_id,
            source_task_revision=task.task_revision, source_attempt_id=attempt.attempt_id,
            source_attempt_fence=attempt.fencing_token, workflow_run_id=run.run_identity,
            workflow_run_revision=run.revision, goal_id=task.goal_id, goal_revision=task.goal_revision,
            capability_id=task.capability_id or "legacy-workflow", capability_version=run.capability_version,
            typed_input_digest=task.typed_input_digest or "", source_context_digest=digest(token),
            evidence_digest=digest(token), artifact_ref=relative, artifact_digest=sha,
            proposal_job_id=f"task-lesson:{binding}", request_idempotency_key=procedure_key or binding,
            request_binding_digest=binding, memory_scope_json=canonical(request.scope.model_dump()),
            source_refs_json=canonical(request.source_refs), provenance_json=canonical({"source_token": token,
                "correction_digest": digest(correction), "positive_preference_vote": False,
                "automatic": _automatic, "automatic_policy": policy}),
            status=MemoryProposalStatus.proposed if candidate else MemoryProposalStatus.blocked,
            reason_code=reason, recovery_action="review_candidate" if candidate else "supply_explicit_correction_and_verified_method_receipt")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        payload = proposal_projection(row)
    _AUTOMATIC_CALLBACKS.pop(binding, None)
    mirror = {"status": "not_requested", "reason_code": "automatic_canonical_receipt"}
    if not _automatic:
        mirror = await _repair_lesson_mirror(row)
    return {**payload, "mirror": mirror}


async def _repair_lesson_mirror(row):
    """Advisory repair cannot hide the committed canonical private proposal."""
    from src.evolution.runtime import EvolutionRuntimeError
    try:
        await asyncio.to_thread(_reconcile_lesson_receipt, row)
    except (EvolutionRuntimeError, OSError):
        return {"status": "degraded", "reason_code": "lesson_mirror_repair_unavailable",
            "recovery_action": "Inspect the canonical candidate; repair or archive the evolution state using its existing owner, then inspect again."}
    return {"status": "reconciled", "reason_code": "lesson_mirror_current"}


def _reconcile_lesson_receipt(row):
    """Recover the idempotent mirror exclusively from committed canonical data."""
    from src.evolution.runtime import EvolutionRuntime
    runtime = EvolutionRuntime(EvolutionRuntime.default_path(settings.workspace_dir))
    runtime.record_task_lesson(proposal_id=row.proposal_id, owner_id=row.owner_principal_id,
        source_digest=row.source_context_digest, candidate_digest=row.artifact_digest,
        proposal_revision=row.revision, result=proposal_projection(row)["result"])


async def propose_automatic_task_lesson(operator, task_id, attempt_id=None):
    """Current authenticated owner callback; finite failure-derived proposals only."""
    source = await eligible_lesson_source(operator, task_id, _automatic=True)
    if attempt_id is not None and source["attempt_id"] != attempt_id:
        return {"status": "blocked", "reason_code": "lesson_attempt_changed", "result": "no_change", "behavior_changed": False}
    if not source["eligible"]:
        return {"status": "blocked", "reason_code": source["reason_code"], "result": "no_change", "behavior_changed": False}
    async with db_engine.get_session() as db:
        await _assert_owner(db, operator, automatic=True)
        task = await _task(db, task_id)
        policy = await _automatic_policy(db, operator, task)
        if not policy["enabled"]:
            return {"status": "blocked", "reason_code": "automatic_lessons_not_opted_in", "result": "no_change", "behavior_changed": False}
        attempt = await db.get(WorkBoardAttempt, source["attempt_id"])
        steps = list((await db.execute(select(WorkflowStepState).where(WorkflowStepState.run_identity == attempt.workflow_run_id))).scalars().all())
        missing_source = source.get("observed", {}).get("status") == "failed" and any(step.error_kind == "FileNotFoundError" for step in steps)
    if not missing_source:
        return {"status": "no_change", "reason_code": "no_supported_failure_lesson", "result": "no_change", "behavior_changed": False}
    return await create_task_lesson(operator, LessonRequest(task_id=task_id, attempt_id=source["attempt_id"],
        correction="Check source existence before using the selected source.", source_refs=source["source_refs"],
        scope=LessonScope.model_validate(source["scope"]), expected_revision=source["expected_revision"]), _automatic=True)


async def maybe_propose_automatic_lesson(task, attempt_id):
    """Called only after committed terminal projection; exact Root, no renewal."""
    from src.auth.service import authenticate_session
    try:
        operator = await authenticate_session(task.owner_session_id, touch=False)
        if operator.principal.principal_id != task.owner_principal_id:
            raise BoardError("lesson_owner_mismatch", "The original task owner changed", status_code=403)
        await _recover_ended_io()
        outcome = await propose_automatic_task_lesson(operator, task.task_id, attempt_id)
    except asyncio.CancelledError:
        async with db_engine.get_session() as db:
            await _begin_sqlite_immediate(db)
            starts = list((await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind == _IO_STARTED,
                WorkBoardEvent.task_id == task.task_id, WorkBoardEvent.owner_principal_id == task.owner_principal_id,
                WorkBoardEvent.owner_session_id == task.owner_session_id))).scalars())
            for start in starts:
                metadata = json.loads(start.metadata_json)
                if metadata["attempt_id"] == attempt_id and metadata["task_revision"] == task.task_revision:
                    await _io_event(db, start, _IO_CANCELLED)
        await _record_automatic_outcome(None, task, {"status": "blocked", "result": "no_change",
            "reason_code": "automatic_lesson_timeout_or_cancelled", "behavior_changed": False}, attempt_id, status_only=True)
        raise
    except Exception as exc:
        outcome = {"status": "blocked", "result": "no_change", "reason_code": "automatic_lesson_unavailable",
            "error_type": type(exc).__name__, "behavior_changed": False}
        await _record_automatic_outcome(None, task, outcome, attempt_id, status_only=True)
        raise
    else:
        await _record_automatic_outcome(operator, task, outcome, attempt_id)
        return outcome
    finally:
        for binding, callback in list(_AUTOMATIC_CALLBACKS.items()):
            if callback is asyncio.current_task():
                _AUTOMATIC_CALLBACKS.pop(binding, None)


async def _record_automatic_outcome(operator, original_task, outcome, attempt_id, *, status_only=False):
    """Append safe outcome metadata under the still-current original owner."""
    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        if not status_only:
            await _assert_owner(db, operator, automatic=True)
        task = await _task(db, original_task.task_id)
        if task is None or (task.owner_principal_id, task.owner_session_id) != (
            original_task.owner_principal_id, original_task.owner_session_id):
            raise BoardError("lesson_task_changed", "Automatic outcome belongs to an older task revision")
        payload = {key: outcome[key] for key in ("status", "result", "reason_code", "proposal_id", "candidate_digest", "error_type", "restart_witness_unknown") if key in outcome}
        payload.update({"task_revision": original_task.task_revision, "behavior_changed": False, "provider_contacts": 0})
        attempt = await db.get(WorkBoardAttempt, attempt_id)
        if attempt is None or attempt.task_id != task.task_id:
            raise BoardError("lesson_attempt_unverified", "The automatic outcome must bind the original attempt")
        if attempt is not None:
            payload.update({"attempt_id": attempt.attempt_id, "workflow_run_id": attempt.workflow_run_id,
                "source_digest": digest({"attempt_id": attempt.attempt_id, "fence": attempt.fencing_token,
                    "ended_at": str(attempt.ended_at), "outcome": attempt.outcome,
                    "receipts": attempt.receipt_refs_json, "task_input_digest": task.typed_input_digest})})
        if outcome.get("proposal_id"):
            proposal = await db.get(MemoryProposal, outcome["proposal_id"])
            if proposal is None or (proposal.owner_principal_id, proposal.owner_session_id, proposal.source_task_id) != (task.owner_principal_id, task.owner_session_id, task.task_id):
                raise BoardError("lesson_owner_mismatch", "Automatic proposal does not bind this owner and task")
            payload.update({"source_digest": proposal.source_context_digest, "proposal_revision": proposal.revision})
        binding = digest(payload)
        rows = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.task_id == task.task_id,
            WorkBoardEvent.owner_principal_id == task.owner_principal_id,
            WorkBoardEvent.owner_session_id == task.owner_session_id,
            WorkBoardEvent.kind == "task_lesson.automatic_outcome.v1"))).scalars().all()
        if any(json.loads(event.metadata_json).get("outcome_binding") == binding for event in rows):
            return
        db.add(WorkBoardEvent(task_id=task.task_id, owner_principal_id=task.owner_principal_id,
            owner_session_id=task.owner_session_id,
            actor_principal_id="service:task-lesson-status" if status_only else operator.principal.principal_id,
            actor_session_id=None if status_only else operator.session_id, kind="task_lesson.automatic_outcome.v1",
            metadata_json=canonical({**payload, "outcome_binding": binding})))


async def inspect_task_lesson(operator, proposal_id):
    async with db_engine.get_session() as db:
        await assert_current_root(db, operator)
        row = await db.get(MemoryProposal, proposal_id, populate_existing=True)
        if (row is None or row.schema_version != PROPOSAL_SCHEMA or
            (row.owner_principal_id, row.owner_session_id) != (operator.principal.principal_id, operator.session_id)):
            raise BoardError("lesson_owner_mismatch", "The lesson belongs to another operator", status_code=403)
        payload = proposal_projection(row)
        ref, sha = row.artifact_ref, row.artifact_digest
    raw = await asyncio.to_thread(read_private_proof, ref, sha)
    mirror = await _repair_lesson_mirror(row)
    envelope = json.loads(raw)
    request = LessonRequest(task_id=payload["task_id"], attempt_id=payload["attempt_id"],
        correction=envelope["correction"], source_refs=envelope["source_refs"], scope=LessonScope.model_validate(envelope["scope"]),
        expected_revision=envelope["source_token"]["task_revision"])
    async with db_engine.get_session() as db:
        try:
            task, attempt, run, token = await _source(db, operator, request)
            candidate = TypeAdapter(Candidate).validate_python(envelope["new_method"]) if envelope.get("new_method") is not None else None
            structured = isinstance(candidate, ResearchStrategy) and task.capability_id == "work.research-dossier.v1"
            if isinstance(candidate, ProcedureCandidateV3):
                from src.memory.task_lesson_native import stage_completed_procedure_source, build_procedure_candidate
                witness = await stage_completed_procedure_source(db, task, run)
                if build_procedure_candidate(witness, envelope["procedure_parameter_selections"]) != candidate:
                    raise BoardError("procedure_candidate_changed", "Restore the original immutable producer candidate")
                audit_token = witness.audit
            else:
                _, audit_token = await _observed_method(db, task, run, request.scope.family, structured_research=structured)
            token["method_receipt"] = audit_token
            current = token == envelope["source_token"]
        except BoardError:
            current = False
    return {**envelope, **payload, "mirror": mirror, "source_current": current, "status": payload["status"] if current else "blocked",
        "reason_code": payload["reason_code"] if current else "lesson_source_changed"}


def proposal_projection(row):
    stored_scope = json.loads(row.memory_scope_json or "{}")
    if stored_scope.get("schema_version") == "task_method_scope.v1":
        scope = {key: stored_scope[key] for key in ("goal_id", "goal_revision", "family")}
    else:
        scope = stored_scope
    return {"proposal_id": row.proposal_id, "schema_version": row.schema_version,
        "task_id": row.source_task_id, "attempt_id": row.source_attempt_id,
        "revision": row.revision, "status": row.status.value, "reason_code": row.reason_code,
        "source_refs": json.loads(row.source_refs_json), "scope": scope,
        "candidate_digest": row.artifact_digest, "behavior_changed": False,
        "result": "candidate_inert" if row.reason_code in {"explicit_correction", "observed_failure_candidate", "explicit_structured_research_method", "explicit_completed_procedure_method"} else "no_change",
        "provider_contact_count": row.provider_contact_count, "quality_evidence": "unmeasured"}


async def eligible_lesson_source(operator, task_id, *, _automatic=False):
    """Authoritative input discovery. Never expose caller-selected evidence."""
    async with db_engine.get_session() as db:
        await _assert_owner(db, operator, automatic=_automatic)
        task = await _task(db, task_id)
        if task is None or (task.owner_principal_id, task.owner_session_id) != (operator.principal.principal_id, operator.session_id):
            raise BoardError("lesson_owner_mismatch", "The task belongs to another operator", status_code=403)
        attempt = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id)
            .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))).scalar_one_or_none()
        payload = {"task_id": task_id, "expected_revision": task.task_revision,
            "attempt_id": attempt.attempt_id if attempt else None, "source_refs": [],
            "scope": {"goal_id": task.goal_id, "goal_revision": task.goal_revision,
                "family": "research" if "research" in (task.capability_id or "") else "general"},
            "eligible": False, "reason_code": "lesson_attempt_unverified", "behavior_changed": False,
            "supported_candidate_kind": None, "source_current": False}
        payload["automatic_policy"] = await _automatic_policy(db, operator, task)
        latest_outcome = (await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.task_id == task_id, WorkBoardEvent.owner_principal_id == task.owner_principal_id,
            WorkBoardEvent.owner_session_id == task.owner_session_id,
            WorkBoardEvent.kind == "task_lesson.automatic_outcome.v1").order_by(WorkBoardEvent.event_id.desc()).limit(1))).scalar_one_or_none()
        outcome = json.loads(latest_outcome.metadata_json) if latest_outcome else None
        payload["automatic_outcome"] = outcome if outcome and outcome.get("task_revision") == task.task_revision else None
        finished = aliased(WorkBoardEvent)
        pending = (await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.kind == _IO_STARTED,
            WorkBoardEvent.task_id == task_id,
            WorkBoardEvent.owner_principal_id == task.owner_principal_id,
            WorkBoardEvent.owner_session_id == task.owner_session_id,
            ~exists(select(finished.event_id).where(finished.kind == _IO_FINISHED,
                finished.mutation_request_digest == WorkBoardEvent.mutation_request_digest)))
            .order_by(WorkBoardEvent.event_id.desc()).limit(1))).scalar_one_or_none()
        payload["restart_witness_unknown"] = bool(pending
            and pending.mutation_request_digest not in _AUTOMATIC_IO
            and json.loads(pending.metadata_json)["process"].get("kind") == "unknown")
        if attempt is None:
            return payload
        run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).scalar_one_or_none()
        refs = [attempt.attempt_id, attempt.workflow_run_id] if attempt.workflow_run_id else []
        if run is not None and run.status == "succeeded":
            from src.memory.m5 import _verified_source, _source_refs
            try:
                proof = await _verified_source(db, task, requested_attempt_id=attempt.attempt_id)
                refs = _source_refs(proof.readback)
            except ValueError:
                return {**payload, "reason_code": "lesson_run_unverified"}
        if not refs:
            return payload
        request = LessonRequest(task_id=task_id, attempt_id=attempt.attempt_id, correction="",
            source_refs=refs, scope=LessonScope.model_validate(payload["scope"]), expected_revision=task.task_revision)
        try:
            _, _, run, token = await _source(db, operator, request, automatic=_automatic)
            structured = not _automatic and task.capability_id == "work.research-dossier.v1" and request.scope.family == "research"
            method, audit = await _observed_method(db, task, run, request.scope.family, structured_research=structured)
        except BoardError as exc:
            return {**payload, "reason_code": exc.code}
        research = structured and audit is not None and token["observed"]["status"] == "completed"
        return {**payload, "source_refs": refs, "eligible": research or method is not None,
            "supported_candidate_kind": "research_strategy" if research else "task_method" if method is not None else None,
            "source_current": True,
            "reason_code": "verified_completed_research_strategy_source" if research else "verified_ordinary_task" if method else "insufficient_method_evidence",
            "observed": token["observed"], "supported_guards": ["source_exists", "verified_readback", "preserve_source_attribution"]}


async def _procedure_source_request(db, operator, task_id):
    await _assert_owner(db, operator)
    task = await _task(db, task_id)
    if task is None or (task.owner_principal_id, task.owner_session_id) != (operator.principal.principal_id, operator.session_id):
        raise BoardError("lesson_owner_mismatch", "The task belongs to another operator", status_code=403)
    if task.capability_id != "agent.task.v1" or task.status.value != "done":
        raise BoardError("procedure_source_not_completed", "Complete and review the original general task first")
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id)
        .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))
    if attempt is None or attempt.ended_at is None:
        raise BoardError("lesson_attempt_unverified", "Use the latest ended native task attempt")
    from src.memory.m5 import _verified_source, _source_refs
    try:
        proof = await _verified_source(db, task, requested_attempt_id=attempt.attempt_id)
    except ValueError as error:
        raise BoardError("procedure_source_unverified", "Restore the original verified physical task output") from error
    return LessonRequest(task_id=task_id, attempt_id=attempt.attempt_id, correction="",
        source_refs=_source_refs(proof.readback), scope=LessonScope(goal_id=task.goal_id,
            goal_revision=task.goal_revision, family="general"), expected_revision=task.task_revision)


async def eligible_procedure_source(operator, task_id):
    """Read-only source and producer offers; never contact or adopt."""
    async with db_engine.get_session() as db:
        await _assert_owner(db, operator)
        task = await _task(db, task_id)
        if task is None or (task.owner_principal_id, task.owner_session_id) != (operator.principal.principal_id, operator.session_id):
            raise BoardError("lesson_owner_mismatch", "The task belongs to another operator", status_code=403)
        payload = {"task_id": task_id, "expected_revision": task.task_revision, "eligible": False,
            "reason_code": "procedure_source_not_completed", "source_attempt": None,
            "parameter_offers": [], "behavior_changed": False, "quality_evidence": "unmeasured"}
        try:
            request = await _procedure_source_request(db, operator, task_id)
            task, attempt, run, token = await _source(db, operator, request)
            from src.memory.task_lesson_native import stage_completed_procedure_source
            witness = await stage_completed_procedure_source(db, task, run)
        except BoardError as error:
            return {**payload, "reason_code": error.code}
        return {**payload, "eligible": True, "reason_code": "verified_completed_procedure_source",
            "source_attempt": attempt.attempt_id, "scope": request.scope.model_dump(mode="json"),
            "source_refs": request.source_refs, "parameter_offers": list(witness.offers),
            "source_receipt": witness.audit, "source_current": True}


async def save_procedure_method(operator, task_id, request: ProcedureSaveRequest):
    """Publish a private immutable inert candidate from original native receipts."""
    async with db_engine.get_session() as db:
        source = await _procedure_source_request(db, operator, task_id)
        if (request.source_attempt, request.expected_revision) != (source.attempt_id, source.expected_revision):
            raise BoardError("procedure_source_changed", "Refresh the exact latest source attempt and task revision")
        task, attempt, run, token = await _source(db, operator, source)
        from src.memory.task_lesson_native import stage_completed_procedure_source, build_procedure_candidate
        witness = await stage_completed_procedure_source(db, task, run)
        try:
            candidate = build_procedure_candidate(witness, request.parameter_selections)
        except (ValueError, KeyError, TypeError) as error:
            raise BoardError("procedure_parameter_selection_invalid", "Select only unique server-offered ordinary fields", status_code=422) from error
    return await create_task_lesson(operator, source, _procedure_candidate=candidate,
        _procedure_witness=witness, _procedure_request=request)
