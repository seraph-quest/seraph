"""Bounded local task continuity; references confer no execution or egress rights."""

import json
from dataclasses import replace

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.ownership import _current_root
from src.db.models import OperatorSession, Session, WorkBoardAttempt, WorkBoardEvent, WorkflowRunState
from src.auth.service import AuthenticatedOperator, AuthFailure, authenticate_session
from src.security.trust_contract import AuthorityGrant, TrustPrincipal
from src.memory.evidence_working_set import _PUBLIC_TYPES, _read_file, read_evidence
from src.memory.evidence_sources import verified_task_outputs
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError, WorkBoardRepository, _UNRESOLVED_RECEIPT_STATUSES, _begin_sqlite_immediate

MAX_CONTEXT_REFS = 32
MAX_TIMELINE = 16


class ContinueTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    expected_revision: int = Field(ge=1)
    new_conversation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class TaskContextPacket(BaseModel):
    task_id: str
    goal_id: str
    conversation_ids: list[str]
    verified_artifact_refs: list[str]
    open_questions: list[str]
    next_actions: list[str]
    private_source_refs: list[str]
    revision: int
    status: str
    summary: str
    summary_kind: str = "factual_canonical_timeline"
    timeline: list[dict[str, str | int]]
    correction_refs: list[str]
    unresolved_effect: str | None
    ownership_access: str
    execution_block_reason: str | None
    evidence_state: str
    source_egress: list[dict[str, str | bool]]
    assistant_context_state: str
    model_context_allowed: bool = False
    memory_status: str = "no_learning"
    truncated: bool


def _load(raw, fallback):
    try:
        return json.loads(raw or "")
    except (ValueError, TypeError):
        return fallback


class TaskContinuityService:
    """Lifecycle-owned projection, with no background work or model transport."""

    def __init__(self, repository: WorkBoardRepository):
        self.repository = repository
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    def _ready(self) -> None:
        if not self._started:
            raise BoardError("task_continuity_unavailable", "Task continuity is inactive; reload after startup", status_code=503)

    async def packet(self, db: AsyncSession, operator: AuthenticatedOperator, task_id: str) -> TaskContextPacket:
        self._ready()
        await _current_root(db, operator)
        owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
        task, read_owner = await self.repository.read_context_task(db, owner, task_id, operator=operator)
        recovered = read_owner != owner
        # A historical transcript is not implicitly selected by selecting a task.
        conversations = list((await db.execute(select(Session.id).where(
            Session.owner_principal_id == owner.principal_id,
            or_(Session.continuity_task_id == task_id, Session.id == task.origin_session_id),
        ).order_by(Session.created_at.desc()).limit(MAX_CONTEXT_REFS + 1))).scalars())
        events = list((await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.task_id == task_id,
            WorkBoardEvent.owner_principal_id == read_owner.principal_id,
            WorkBoardEvent.owner_session_id == read_owner.session_id,
        ).order_by(WorkBoardEvent.event_id.desc()).limit(MAX_TIMELINE + 1))).scalars())
        # Whitelisted factual metadata only: never arbitrary event text or intent.
        timeline = []
        correction_refs = []
        for event in reversed(events[:MAX_TIMELINE]):
            metadata = _load(event.metadata_json, {})
            if not isinstance(metadata, dict):
                metadata = {}
            item = {"event_id": event.event_id, "at": event.created_at.isoformat()}
            if event.kind in {"task.created", "task.updated", "task.blocked", "task.unblocked", "task.completed", "comment.created", "task.cancelled", "task.review_requested"}:
                item["kind"] = event.kind
            else:
                item["kind"] = "canonical_task_event"
            if metadata.get("status") in {"triage", "todo", "ready", "running", "blocked", "review", "done", "cancelled"}:
                item["status"] = metadata["status"]
            if type(metadata.get("task_revision")) is int:
                item["revision"] = metadata["task_revision"]
            timeline.append(item)
            if event.kind == "comment.created":
                correction_refs.append(f"task-event:{event.event_id}")
        evidence_state = "available"
        private_refs = []
        source_egress = []
        try:
            evidence = await read_evidence(db, owner, task_id, operator=operator)
            private_refs = [claim["source_id"] for claim in evidence["claims"]
                            if claim["private_source"]][:MAX_CONTEXT_REFS]
            source_egress = [{"source_id": claim["source_id"], "private_source": claim["private_source"],
                "model_context_allowed": not recovered and AuthorityGrant.MODEL_INFERENCE in operator.principal.grants
                    and evidence["allow_model_context"] and claim["model_context_allowed"]}
                for claim in evidence["claims"]][:MAX_CONTEXT_REFS]
            if evidence["invalidated_count"]:
                evidence_state = "changed_or_deleted_sources"
            elif not evidence["revision"]:
                evidence_state = "no_selected_evidence"
        except BoardError as exc:
            if exc.code not in {"evidence_packet_unavailable", "evidence_packet_invalid"}:
                raise
            evidence_state = exc.code
        attempts = list((await db.execute(select(WorkBoardAttempt).where(
            WorkBoardAttempt.task_id == task_id,
        ).order_by(WorkBoardAttempt.created_at.desc()).limit(MAX_CONTEXT_REFS))).scalars())
        verified = []
        selected_outputs = {}
        if recovered:
            from src.auth.ownership import selected_read_scopes
            selected_outputs = await selected_read_scopes(operator, "output_artifact", db=db)
        unresolved = task.block_kind if task.block_kind in {"unknown_effect", "cost_liability", "reconcile_admission_binding"} else None
        for attempt in attempts:
            if attempt.outcome in {"unknown", "unknown_external_effect", "cost_liability"}:
                unresolved = "unknown_external_effect"
            receipts = _load(attempt.receipt_refs_json, [])
            if isinstance(receipts, list) and any(
                isinstance(receipt, dict) and receipt.get("status") in _UNRESOLVED_RECEIPT_STATUSES
                for receipt in receipts[:32]
            ):
                unresolved = "unknown_external_effect"
            run = (await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.run_identity == attempt.workflow_run_id,
            ))).scalar_one_or_none() if attempt.workflow_run_id else None
            if run is None:
                continue
            # Validate complete canonical lineage and output bytes before exposing a ref.
            outputs = await verified_task_outputs(db, task, attempt, run) if run.status == "succeeded" and attempt.outcome == "verified" else []
            for _, receipt in outputs:
                ref = receipt.get("artifact_id")
                if receipt.get("artifact_type") not in _PUBLIC_TYPES:
                    continue
                if recovered and selected_outputs.get(ref) != read_owner.session_id:
                    continue
                try:
                    _read_file(receipt["file_path"], receipt["content_sha256"])
                except (OSError, ValueError, KeyError):
                    evidence_state = "changed_or_deleted_sources"
                    continue
                if isinstance(ref, str) and len(ref) <= 256 and ref not in verified:
                    verified.append(ref)
        if recovered:
            actions = ["Review current scope in Work before any execution or egress"]
        elif unresolved:
            actions = ["Open Work and reconcile the unresolved effect; do not retry contact"]
        elif task.status.value in {"done", "cancelled"}:
            actions = ["Review the recorded outcome in Work"]
        elif task.block_kind == "needs_input":
            actions = ["Open Work and supply the missing input"]
        else:
            actions = ["Open Work and review current task controls"]
        questions = ["What input is required to unblock this task? Review its current blocker in Work."] if task.block_kind == "needs_input" else []
        return TaskContextPacket(
            task_id=task_id, goal_id=task.goal_id, revision=task.task_revision,
            status=task.status.value, summary=f"Task is {task.status.value}; {len(verified)} verified output reference(s).",
            conversation_ids=conversations[:MAX_CONTEXT_REFS], verified_artifact_refs=verified[:MAX_CONTEXT_REFS],
            open_questions=questions, next_actions=actions, private_source_refs=private_refs,
            timeline=timeline, correction_refs=correction_refs, unresolved_effect=unresolved,
            ownership_access="recovered_read_only" if recovered else "current",
            execution_block_reason="current_scope_review_required" if recovered else None,
            evidence_state=evidence_state,
            source_egress=source_egress,
            assistant_context_state="current_scope_review_required" if recovered else (
                "ready_reference_only" if AuthorityGrant.MODEL_INFERENCE in operator.principal.grants else "current_model_grant_required"),
            truncated=len(events) > MAX_TIMELINE or len(conversations) > MAX_CONTEXT_REFS or len(verified) > MAX_CONTEXT_REFS,
        )

    async def for_chat(self, db: AsyncSession, conversation_id: str, principal: TrustPrincipal | None) -> str:
        """One current-authority compiler used by both existing chat paths."""
        if not self._started:
            return "Task continuity is inactive. Reload current task context in Work; no historical intent was replayed."
        conversation = await db.get(Session, conversation_id)
        if conversation is None or not conversation.continuity_task_id:
            return ""
        blocked = "Task continuation context is blocked. Review current task scope and model egress in Work; no historical action or source was replayed."
        if (principal is None or not principal.authenticated or principal.revoked
            or principal.principal_id != conversation.owner_principal_id
            or principal.session_id != conversation_id or not principal.operator_session_id
            or AuthorityGrant.MODEL_INFERENCE not in principal.grants):
            return blocked
        try:
            operator = await authenticate_session(principal.operator_session_id, touch=False)
            if operator.principal.principal_id != principal.principal_id:
                return blocked
            # A trusted runtime session read is not a bearer ingress. Bind its
            # freshly authenticated exact row only inside this server-owned seam.
            root = await db.get(OperatorSession, operator.session_id, populate_existing=True)
            if root is None:
                return blocked
            operator = replace(operator, _token_hash=root.token_hash)
            packet = await self.packet(db, operator, conversation.continuity_task_id)
        except (AuthFailure, BoardError):
            return blocked
        if packet.ownership_access != "current":
            return blocked
        allowed_refs = [source["source_id"] for source in packet.source_egress if source["model_context_allowed"]]
        projection = {"task_id": packet.task_id, "goal_id": packet.goal_id, "revision": packet.revision,
            "status": packet.status, "verified_artifact_refs": packet.verified_artifact_refs,
            "selected_source_refs": allowed_refs, "open_questions": packet.open_questions,
            "next_actions": packet.next_actions, "unresolved_effect": packet.unresolved_effect}
        return ("--- CANONICAL TASK CONTINUITY (read-only facts and permitted references; no execution authority) ---\n"
            + json.dumps(projection, sort_keys=True, separators=(",", ":")))

    async def continue_task(self, db: AsyncSession, operator: AuthenticatedOperator, request: ContinueTaskRequest) -> dict:
        self._ready()
        await _begin_sqlite_immediate(db)
        packet = await self.packet(db, operator, request.task_id)
        if packet.revision != request.expected_revision:
            raise BoardError("task_context_revision_stale", "Reload current task context; old chat intent was not replayed", status_code=409)
        conversation = await db.get(Session, request.new_conversation_id)
        if conversation is not None:
            if (conversation.owner_principal_id != operator.principal.principal_id
                or conversation.continuity_task_id != request.task_id):
                raise BoardError("task_conversation_conflict", "Choose a new conversation identity", status_code=409)
            replay = True
        else:
            conversation = Session(id=request.new_conversation_id,
                owner_principal_id=operator.principal.principal_id,
                continuity_task_id=request.task_id, title="Continue task")
            db.add(conversation)
            await db.flush()
            replay = False
        packet = await self.packet(db, operator, request.task_id)
        return {"conversation_id": conversation.id, "task_context": packet.model_dump(), "idempotent_replay": replay}
