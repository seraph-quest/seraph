"""Fresh C1 invocation of an exact current reviewed general method.

The private gate commits request identity to the existing task event owner.
It neither reserves a budget nor grants execution authority.
"""
from dataclasses import dataclass
import json

from pydantic import Field
from sqlalchemy import select

from src.db import engine as database
from src.db.models import WorkBoardTask, WorkBoardEvent, WorkBoardInputArtifact
from src.memory.procedure_recommendations import canonical, digest
from src.memory.task_methods import (Closed, Id, Sha, ActiveMethodBinding,
    _context, _pointer, _pointer_valid, _fail, _stage)
from src.work_board.contracts import (TaskLimits, GeneralTaskInput, GeneralTaskCreate,
    WorkBoardOwner, GeneralTaskEnvelope)
from src.work_board.repository import _begin_sqlite_immediate, BoardError

INVOCATION_EVENT = "task_method.invocation.v3"


class TaskMethodInvoke(Closed):
    version: Id
    digest: Sha
    expected_pointer_revision: int = Field(ge=1)
    goal_id: Id
    goal_revision: int = Field(ge=1)
    parameters: dict[str, str | int | bool | None] = Field(max_length=16)
    limits: TaskLimits
    inference_egress_acknowledged: bool
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass
class _InvocationGate:
    owner: WorkBoardOwner
    proposal_id: str
    request: TaskMethodInvoke
    request_digest: str
    binding: object = None
    envelope: object = None

    async def current(self, db):
        # Resolve the lifecycle owner dynamically; restart/tests may replace
        # the owner instance without replacing this module.
        from src.memory.task_methods import current_method
        if not current_method._started or current_method._key is None:
            _fail("method_lifecycle_not_started")
        identity, scope = await _context(db, self.owner, self.request.goal_id, "general")
        if scope.goal_revision != self.request.goal_revision:
            _fail("method_invocation_goal_changed")
        pointer = await _pointer(db, identity, scope)
        if (pointer is None or pointer.baseline or not _pointer_valid(pointer, current_method._key)
            or pointer.revision != self.request.expected_pointer_revision):
            _fail("method_invocation_pointer_changed")
        selected = ActiveMethodBinding.model_validate_json(pointer.binding_json)
        if (selected.proposal_id, selected.version, selected.digest) != (
            self.proposal_id, self.request.version, self.request.digest):
            _fail("method_invocation_noncurrent_version")
        binding = await current_method._version(db, selected, identity, scope, allow_rollback=False)
        if binding.typed_data.get("schema_version") != "ProcedurePlan.v3":
            _fail("method_invocation_schema_unsupported")
        if self.binding is not None and binding != self.binding:
            _fail("method_invocation_pin_changed")
        self.binding = binding
        return binding

    def stage_envelope(self, envelope):
        if envelope.strategy != self.binding:
            _fail("method_invocation_pin_changed")
        from src.workflows.procedure_contracts import ProcedureCandidateV3, validate_procedure_plan_instance
        candidate = ProcedureCandidateV3.model_validate(self.binding.typed_data)
        values = validate_procedure_plan_instance(candidate.plan, envelope.plan, envelope.task_input.requested_output)
        if values != self.request.parameters:
            _fail("method_invocation_parameters_changed")
        expected_input = GeneralTaskInput(goal_ref=self.request.goal_id,
            intent="Invoke reviewed general-task method", requested_output=candidate.plan.output_contract,
            limits=self.request.limits, tool_set_digest=envelope.task_input.tool_set_digest,
            inference_egress_acknowledged=self.request.inference_egress_acknowledged)
        if envelope.task_input != expected_input:
            _fail("method_invocation_input_changed")
        self.envelope = envelope

    def _identity(self):
        return {"request_digest": self.request_digest,
            "proposal_id": self.proposal_id, "version": self.request.version,
            "digest": self.request.digest, "pointer_revision": self.request.expected_pointer_revision}

    async def check(self, db, task=None, *, original=None):
        await self.current(db)
        if task is None:
            if self.envelope is None:
                _fail("method_invocation_envelope_missing")
            self.stage_envelope(self.envelope)
            return
        events = list((await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.task_id == task.task_id, WorkBoardEvent.kind == INVOCATION_EVENT,
            WorkBoardEvent.owner_principal_id == self.owner.principal_id,
            WorkBoardEvent.owner_session_id == self.owner.session_id).limit(2))).scalars())
        if len(events) != 1:
            _fail("method_invocation_binding_missing")
        event = events[0]
        data = json.loads(event.metadata_json)
        if (data.get("identity") != self._identity()
            or data.get("typed_input_digest") != task.typed_input_digest
            or data.get("typed_input_ref") != task.typed_input_ref
            or task.goal_id != self.request.goal_id or task.goal_revision != self.request.goal_revision
            or task.idempotency_scope != "general-task" or task.idempotency_key != self.request.idempotency_key):
            _fail("method_invocation_idempotency_conflict")
        artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id, populate_existing=True)
        if (artifact is None or artifact.bound_task_id != task.task_id
            or artifact.payload_sha256 != task.typed_input_digest or artifact.typed_input_ref != task.typed_input_ref):
            _fail("method_invocation_artifact_changed")
        if original is not None:
            self.stage_envelope(original)
            if digest(original.model_dump(mode="json")) != data.get("envelope_digest"):
                _fail("method_invocation_envelope_changed")
        elif data.get("strategy_digest") != digest(self.binding.model_dump(mode="json")):
            _fail("method_invocation_pin_changed")

    async def publish(self, db, task):
        await self.check(db)
        db.add(WorkBoardEvent(task_id=task.task_id,
            owner_principal_id=self.owner.principal_id, owner_session_id=self.owner.session_id,
            actor_principal_id=self.owner.principal_id, actor_session_id=self.owner.session_id,
            kind=INVOCATION_EVENT, metadata_json=canonical({"identity": self._identity(),
                "typed_input_digest": task.typed_input_digest, "typed_input_ref": task.typed_input_ref,
                "envelope_digest": digest(self.envelope.model_dump(mode="json")),
                "strategy_digest": digest(self.binding.model_dump(mode="json"))})))
        await db.flush()


async def invoke_method(operator, proposal_id, request, service):
    from src.auth.service import AuthFailure
    from sqlalchemy.exc import SQLAlchemyError
    from src.extensions.capability_execution import CapabilityJournalError
    try:
        return await _invoke_method(operator, proposal_id, request, service)
    except AuthFailure as error:
        raise BoardError("method_current_owner_required", "Authenticate the current method owner", status_code=403) from error
    except (OSError, SQLAlchemyError, CapabilityJournalError) as error:
        raise BoardError("method_invocation_store_unavailable", "Restore canonical method storage and private source artifacts", status_code=503) from error


async def _invoke_method(operator, proposal_id, request, service):
    from src.workflows.procedure_contracts import ProcedureCandidateV3, instantiate_procedure_plan
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    gate = _InvocationGate(owner, proposal_id, request,
        digest({"owner": owner.model_dump(mode="json"), "proposal_id": proposal_id,
            "request": request.model_dump(mode="json")}))
    async with database.get_session() as db:
        await _begin_sqlite_immediate(db)
        binding = await gate.current(db)
        existing = await db.scalar(select(WorkBoardTask).where(
            WorkBoardTask.owner_principal_id == owner.principal_id,
            WorkBoardTask.owner_session_id == owner.session_id,
            WorkBoardTask.idempotency_scope == "general-task",
            WorkBoardTask.idempotency_key == request.idempotency_key))
        if existing is not None:
            await gate.check(db, existing)
            # Preserve only this fully loaded original row for off-writer file
            # verification. Rollback expires attached SQLAlchemy instances;
            # the later writer re-fetches and checks canonical metadata again.
            db.expunge(existing)
        await db.rollback()
    source_stage = await _stage(operator, proposal_id, acceptance=True)
    if (source_stage.candidate.model_dump(mode="json") != binding.typed_data
        or not source_stage.consumer_supported):
        _fail("method_invocation_source_changed")
    if existing is not None:
        # Filesystem reads cannot run under the canonical publication writer.
        from src.work_board.dispatcher import _parse_typed_input
        original = GeneralTaskEnvelope.model_validate(_parse_typed_input(existing))
        async with database.get_session() as db:
            await _begin_sqlite_immediate(db)
            fresh = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == existing.task_id)
                .execution_options(populate_existing=True))
            if fresh is None:
                _fail("method_invocation_task_missing")
            await gate.check(db, fresh, original=original)
            return {"task_id": fresh.task_id, "idempotent_replay": True}
    candidate = ProcedureCandidateV3.model_validate(binding.typed_data)
    plan = instantiate_procedure_plan(candidate.plan, request.parameters)
    _, tool_digest = service.snapshot()
    task_input = GeneralTaskInput(goal_ref=request.goal_id, intent="Invoke reviewed general-task method",
        requested_output=candidate.plan.output_contract, limits=request.limits,
        tool_set_digest=tool_digest, inference_egress_acknowledged=request.inference_egress_acknowledged)
    body = GeneralTaskCreate(input=task_input, plan=plan, goal_revision=request.goal_revision,
        expected_plan_revision=1, idempotency_key=request.idempotency_key)
    async with database.get_session() as db:
        mutation = await service.create(db, owner, body, _procedure_invocation=gate)
        return {"task_id": mutation.task.task_id, "idempotent_replay": mutation.idempotent_replay}
