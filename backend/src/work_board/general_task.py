"""Closed registered-tool task interpreter on the existing board and job owners.

Planning is data only. This service neither creates another queue nor starts
an agent/model loop; proposal acceptance and durable admission are distinct.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import inspect
import json
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from src.work_board.contracts import (
    DependencyPointer, GeneralTaskCreate, GeneralTaskEnvelope, PlanSpec,
    StrategyResolver, TaskStrategyBinding, ToolDescriptor, WorkBoardInputArtifactCreate,
    WorkBoardOwner, WorkBoardTaskCreate,
)
from src.work_board.repository import BoardError, WorkBoardRepository
from src.db.models import WorkBoardStatus

CAPABILITY = "agent.task.v1"
MAX_BYTES = 64 * 1024


@contextmanager
def current_task_service(*, registry=None, dispatcher=None, planner=None):
    """Current Python lifecycle, with cleanup even before app readiness."""
    if dispatcher is None:
        from src.work_board.dispatcher import _dispatcher
        dispatcher = _dispatcher
    if registry is None:
        from src.native_tools.registry import ToolRegistry
        from src.tools.mcp_manager import mcp_manager
        from src.extensions.registry import extension_registry
        registry = ToolRegistry(mcp_runtime=mcp_manager, extension_registry=extension_registry)
    if planner is None:
        from src.work_board.general_task_planner import GeneralTaskPlanner
        planner = GeneralTaskPlanner()
    service = GeneralTaskService(registry, planner=planner)
    if dispatcher.general_tasks is not None:
        raise RuntimeError("general task lifecycle already owned")
    try:
        registry.start()
        service.start()
        dispatcher.general_tasks = service
        yield service
    finally:
        if dispatcher.general_tasks is service:
            dispatcher.general_tasks = None
        service.stop()
        registry.stop()


def canonical(value: Any) -> bytes:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise BoardError("general_task_payload_limit", "Task data exceeds 64 KiB", status_code=422)
    return encoded


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_schema(schema: dict, value: Any = None, *, check_value: bool = True):
    """Local schemas only: no reference retrieval, executable expressions or secrets."""
    def closed(item, depth=0):
        if depth > 32:
            raise ValueError("schema nesting limit")
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    raise ValueError("schema references are excluded")
                closed(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                closed(child, depth + 1)
    canonical(schema)
    closed(schema)
    from src.work_board.general_task_schema import validate_safe_patterns
    validate_safe_patterns(schema)
    Draft202012Validator.check_schema(schema)
    if check_value:
        Draft202012Validator(schema).validate(value)


def validate_data(value: Any, *, dependencies: set[str], depth: int = 0):
    from src.work_board.dispatcher import _AUTHORITY_INPUT_KEYS
    if depth > 32:
        raise ValueError("task data nesting limit")
    canonical(value)
    if isinstance(value, dict):
        if "$dependency" in value:
            if set(value) != {"$dependency"}:
                raise ValueError("dependency pointers cannot carry sibling fields")
            pointer = DependencyPointer.model_validate(value["$dependency"])
            if pointer.step_id not in dependencies:
                raise ValueError("pointer requires a declared dependency")
            return
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _AUTHORITY_INPUT_KEYS or normalized in {
                "password", "secret", "secret_ref", "api_key", "token", "authorization",
                "credential", "credentials", "credential_refs", "expression", "eval", "shell",
            } or str(key).startswith("$"):
                raise ValueError("privilege-bearing or executable task data is excluded")
            validate_data(child, dependencies=dependencies, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            validate_data(child, dependencies=dependencies, depth=depth + 1)
    elif isinstance(value, str) and ("${vault:" in value or value.startswith("secret-ref:")):
        raise ValueError("secret references do not belong in planner data")


def has_pointer(value):
    if isinstance(value, dict):
        return "$dependency" in value or any(has_pointer(item) for item in value.values())
    if isinstance(value, list):
        return any(has_pointer(item) for item in value)
    return False


def resolve_input(value, verified_outputs):
    if isinstance(value, dict):
        if "$dependency" in value:
            pointer = DependencyPointer.model_validate(value["$dependency"])
            selected = verified_outputs[pointer.step_id]
            if pointer.pointer:
                for segment in pointer.pointer[1:].split("/"):
                    segment = segment.replace("~1", "/").replace("~0", "~")
                    if isinstance(selected, list):
                        if not segment.isdigit() or (segment != "0" and segment.startswith("0")):
                            raise ValueError("invalid array pointer")
                        selected = selected[int(segment)]
                    elif isinstance(selected, dict):
                        selected = selected[segment]
                    else:
                        raise ValueError("pointer does not name a JSON value")
            # Detach data from its verified artifact representation.
            return json.loads(canonical(selected))
        return {key: resolve_input(item, verified_outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_input(item, verified_outputs) for item in value]
    return value


async def write_step_artifact(jobs, *, job_id, owner, fence, plan_digest, step_id, output, authority_check=None):
    """Task-specific checkpoint fields on the existing private artifact owner."""
    from src.work_board.input_artifacts import _write_payload, _safe_file_bytes
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    if authority_check is not None:
        async with jobs._session() as db:
            run = await jobs._fetch(db, job_id)
            jobs._assert_lease(run, owner=owner, fencing_token=fence)
            await authority_check(db, run)
    content = canonical({"step_id": step_id, "output": output})
    sha = hashlib.sha256(content).hexdigest()
    key = digest([job_id, plan_digest, step_id])
    reference = f"artifacts/work-board/general-tasks/{key}-{sha}.json"
    binding = {"schema_version": 1, "producer_ref": job_id, "step_id": step_id,
        "plan_digest": plan_digest, "producer_fence": fence, "file_path": reference,
        "content_sha256": sha, "size_bytes": len(content), "no_learning": True}
    await jobs.record_checkpoint(job_id, checkpoint_id="general:artifact:" + step_id,
        state=binding, checkpoint_payload=binding, owner=owner, fencing_token=fence)
    path = canonical_workspace_root(settings.workspace_dir) / reference
    _write_payload(path, content)
    actual = _safe_file_bytes(path, expected_digest=sha, expected_size=len(content))
    if actual != content:
        raise BoardError("general_task_artifact_changed", "Task output failed physical readback", status_code=409)
    await jobs.record_artifact(job_id, file_path=reference, artifact_type="general_task_step",
        content=actual, owner=owner, fencing_token=fence)
    await jobs.record_readback(job_id, effect_type="general_task_artifact_readback",
        target_path=reference, target_digest=sha, content_sha256=sha,
        status="succeeded", readback_id="general-artifact:" + key[:32],
        verified_at=datetime.now(timezone.utc).isoformat(),
        details={"verified": True, "output_exists": True, "no_learning": True},
        owner=owner, fencing_token=fence, **({"readback_authority_check": authority_check} if authority_check is not None else {}))
    return binding, json.loads(actual)["output"]


class DescriptorRegistry(Protocol):
    def descriptors(self) -> list[ToolDescriptor]: ...
    async def invoke(self, descriptor: ToolDescriptor, inputs: dict, *, principal,
                     job_id: str, fencing_token: int) -> Any: ...


class GeneralTaskService:
    def __init__(self, registry: DescriptorRegistry, *, repository=None,
                 strategy_resolver: StrategyResolver | None = None, planner=None,
                 repository_source_service=None):
        self.registry = registry
        self.repository = repository or WorkBoardRepository()
        self.strategy_resolver = strategy_resolver
        self.planner = planner
        self.repository_source_service = repository_source_service
        self.started = False
        # Ephemeral original callback handles, never execution authority. They
        # remain inspectable after waiter cancellation until the callback exits.
        self._native_invocations = {}
        self._native_invocation_bindings = {}
        self._native_output_root_witnesses = {}
        self._native_cancel_observers = set()

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    async def observe_native_cancellation(self, jobs, parent_id):
        """Reduce cancellation debt using retained original callbacks only."""
        from src.db.models import WorkBoardAttempt
        from src.workflows.job_runtime import DurableJobError
        bindings = [(child_id, binding, self._native_invocations.get(child_id))
            for child_id, binding in list(self._native_invocation_bindings.items())
            if binding.parent_job_id == parent_id]
        result = None
        for child_id, binding, handle in bindings:
            if handle is None or not handle.closed:
                continue
            async with jobs._session() as db:
                attempt = await db.get(WorkBoardAttempt, binding.attempt_id, populate_existing=True)
                if attempt is None or attempt.cancel_requested_at is None:
                    continue
            try:
                result = await jobs.observe_general_task_native_cancel_closure(child_id,
                    producer_witness=handle.witness,
                    output_root_witness=self._native_output_root_witnesses.get(child_id))
            except (BoardError, DurableJobError):
                # Invalid/corrupt canonical cancellation remains debt. Keep
                # the original closed producer available for reconciliation.
                continue
            if result.get("cancellation", {}).get("state") == "fully_cancelled":
                self.release_native_invocation(child_id)
        return result

    def retain_native_invocation(self, jobs, binding, invocation, *, output_root_witness=None):
        import asyncio
        self._native_invocations[binding.invocation_id] = invocation
        self._native_invocation_bindings[binding.invocation_id] = binding
        self._native_output_root_witnesses[binding.invocation_id] = output_root_witness
        def closed(_original):
            async def observe():
                try:
                    await self.observe_native_cancellation(jobs, binding.parent_job_id)
                except Exception:
                    # Shutdown/storage failure cannot manufacture closure or
                    # discard the source producer. A later observation may
                    # retry the same cleanup-only canonical publication.
                    return
            task = asyncio.create_task(observe())
            self._native_cancel_observers.add(task)
            task.add_done_callback(self._native_cancel_observers.discard)
        invocation.on_closed(closed)

    def release_native_invocation(self, child_id):
        from src.work_board.general_task_runtime_artifacts import release_native_cancel_output_witness
        release_native_cancel_output_witness(self._native_output_root_witnesses.pop(child_id, None))
        self._native_invocations.pop(child_id, None)
        self._native_invocation_bindings.pop(child_id, None)

    def snapshot(self):
        if not self.started:
            raise BoardError("general_task_inactive", "Task service is inactive", status_code=503)
        descriptors = sorted(self.registry.descriptors(), key=lambda item: item.tool_id)
        if len({item.tool_id for item in descriptors}) != len(descriptors):
            raise BoardError("general_task_tool_identity_conflict", "Duplicate tool identity", status_code=409)
        return descriptors, digest([item.model_dump(mode="json") for item in descriptors])

    async def strategy(self, owner, goal_ref):
        binding = TaskStrategyBinding(status="none", reason="baseline")
        if self.strategy_resolver is not None:
            binding = self.strategy_resolver.resolve(owner, goal_ref, CAPABILITY)
            if inspect.isawaitable(binding):
                binding = await binding
            binding = TaskStrategyBinding.model_validate(binding)
        if binding.status == "blocked":
            raise BoardError("general_task_strategy_blocked", binding.reason, status_code=409)
        return binding

    async def evidence(self, db, owner, references):
        """Only current owner-completed board output is execution evidence."""
        from sqlalchemy import select
        from src.db.models import WorkBoardAttempt, WorkflowRunState
        from src.work_board.review import _verified_workflow_readback
        from src.work_board.input_artifacts import _safe_file_bytes
        from src.workspace import canonical_workspace_root
        from config.settings import settings
        result = []
        for reference in references:
            if not reference.startswith("board-output:"):
                raise BoardError("general_task_evidence_unsupported", "Select verified completed Work output", status_code=422)
            producer = await self.repository.get_task(db, owner, reference.removeprefix("board-output:"))
            if producer.status != WorkBoardStatus.done:
                raise BoardError("general_task_evidence_stale", "Evidence producer is not complete", status_code=409)
            attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == producer.task_id)
                .order_by(WorkBoardAttempt.created_at.desc()).limit(1))
            if attempt is None or await _verified_workflow_readback(db, producer, attempt) is None:
                raise BoardError("general_task_evidence_unverified", "Evidence needs verified readback", status_code=409)
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))
            if run is None or run.status != "succeeded":
                raise BoardError("general_task_evidence_unverified", "Evidence run has not succeeded", status_code=409)
            artifacts, effects = json.loads(run.artifact_receipts_json), json.loads(run.effect_receipts_json)
            artifact = next((item for item in artifacts if item.get("exists") and any(
                effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded"
                and effect.get("target_path") == item.get("file_path")
                and effect.get("content_sha256") == item.get("content_sha256") for effect in effects)), None)
            if not artifact:
                raise BoardError("general_task_evidence_unverified", "Evidence artifact lacks readback", status_code=409)
            path = artifact["file_path"]
            if path.startswith(("/", "~")) or ".." in path.split("/"):
                raise BoardError("general_task_evidence_unverified", "Evidence path is invalid", status_code=409)
            size = artifact.get("size_bytes") or artifact.get("byte_count")
            if type(size) is not int or not 0 < size <= MAX_BYTES:
                raise BoardError("general_task_evidence_limit", "Evidence exceeds the finite data allowance", status_code=409)
            raw = _safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / path,
                expected_digest=artifact["content_sha256"], expected_size=size)
            result.append({"reference": reference, "producer_revision": producer.task_revision,
                "producer_attempt_ref": attempt.attempt_id, "file_path": path,
                "content_sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": size})
        return result

    async def validate(self, owner: WorkBoardOwner, request: GeneralTaskCreate):
        descriptors, tool_digest = self.snapshot()
        if request.plan is None:
            raise BoardError("general_task_plan_required", "Generate and review a typed plan first", status_code=422)
        if request.input.tool_set_digest != tool_digest:
            raise BoardError("general_task_tool_set_changed", "Refresh the current tool contract", status_code=409)
        by_id = {item.tool_id: item for item in descriptors}
        selected = {}
        try:
            from src.work_board.general_task_schema import schema_accepts_output
            validate_schema(request.input.requested_output, check_value=False)
            for step in request.plan.steps:
                descriptor = by_id.get(step.tool_id)
                if descriptor is None:
                    raise ValueError("tool unavailable: " + step.tool_id)
                validate_schema(descriptor.input_schema, check_value=False)
                validate_schema(descriptor.output_schema, check_value=False)
                validate_schema(step.output_contract, check_value=False)
                if not schema_accepts_output(descriptor.output_schema, step.output_contract):
                    raise ValueError("step output contract is incompatible")
                validate_data(step.input, dependencies=set(step.depends_on))
                if not has_pointer(step.input):
                    validate_schema(descriptor.input_schema, step.input)
                selected[step.tool_id] = descriptor
            final = request.plan.steps[-1]
            if not schema_accepts_output(final.output_contract, request.input.requested_output):
                raise ValueError("requested output contract is incompatible")
        except Exception as exc:
            raise BoardError("general_task_plan_invalid", "Plan violates a registered tool contract", status_code=422) from exc
        strategy = await self.strategy(owner, request.input.goal_ref)
        envelope = GeneralTaskEnvelope(task_input=request.input, plan=request.plan,
            descriptors=list(selected.values()), strategy=strategy)
        from src.work_board.document_preparation import check_envelope
        check_envelope(envelope)
        if any(step.tool_id == "document_prepare" for step in request.plan.steps) and request.input.document_source is None:
            raise BoardError("document_local_consent_required", "Explicit source selection is required", status_code=422)
        return envelope

    async def create(self, db, owner, request: GeneralTaskCreate, *, publication_authority_check=None,
                     publication_authority_scope=None):
        def reject_repository_scope(plan):
            if (plan is not None and any(step.tool_id == "repository_work" for step in plan.steps)
                    and (publication_authority_scope is not None or publication_authority_check is not None)):
                raise BoardError("repository_publication_scope_incompatible",
                    "Repository Tasks require their single source-owned publication scope", status_code=409)
        # Literal repository requests fail before inspection, artifact staging,
        # planning, or database access when an unrelated scope is supplied.
        reject_repository_scope(request.plan)
        if request.input.document_source is not None and request.plan is None:
            raise BoardError("document_local_plan_required", "Document preparation requires an explicit local plan", status_code=422)
        from src.work_board.input_artifacts import prepare_input_artifact
        from sqlalchemy import select
        from src.db.models import WorkBoardTask, WorkBoardEvent
        from src.work_board.repository import BoardMutation
        from src.work_board.dispatcher import _parse_typed_input
        existing = await db.scalar(select(WorkBoardTask).where(
            WorkBoardTask.owner_principal_id == owner.principal_id,
            WorkBoardTask.owner_session_id == owner.session_id,
            WorkBoardTask.idempotency_scope == "general-task",
            WorkBoardTask.idempotency_key == request.idempotency_key))
        if existing is not None:
            original = GeneralTaskEnvelope.model_validate(_parse_typed_input(existing))
            reject_repository_scope(original.plan)
            compared_input = request.input.model_copy(update={"tool_set_digest":
                request.input.tool_set_digest or original.task_input.tool_set_digest})
            if (compared_input != original.task_input or request.goal_revision != existing.goal_revision
                or (request.plan is not None and request.plan != original.plan)):
                raise BoardError("general_task_idempotency_conflict", "Request key identifies different task data", status_code=409)
            event = await db.scalar(select(WorkBoardEvent).where(WorkBoardEvent.task_id == existing.task_id)
                .order_by(WorkBoardEvent.event_id.desc()).limit(1))
            if event is None:
                raise BoardError("general_task_event_unavailable", "Task publication needs recovery", status_code=409)
            if original.plan is not None and any(step.tool_id == "repository_work" for step in original.plan.steps):
                original_task_row = existing.model_dump(mode="json")
                from src.workflows.repo_repair_source import prepare_repository_task_publication
                from src.work_board.repository import _begin_sqlite_immediate
                from src.work_board.input_artifacts import read_input_artifact_metadata
                from contextlib import AsyncExitStack
                _, source_check, source_scope = prepare_repository_task_publication(
                    self.repository_source_service, original, owner=owner,
                    goal_revision=request.goal_revision, replay=True)
                async with AsyncExitStack() as scopes:
                    await scopes.enter_async_context(source_scope())
                    if publication_authority_scope is not None:
                        await scopes.enter_async_context(publication_authority_scope())
                    await _begin_sqlite_immediate(db)
                    await source_check(db)
                    if publication_authority_check is not None:
                        await publication_authority_check(db)
                    artifact = await read_input_artifact_metadata(db, owner,
                        artifact_id=existing.input_artifact_id)
                    fresh = await db.scalar(select(WorkBoardTask).where(
                        WorkBoardTask.task_id == existing.task_id).execution_options(populate_existing=True))
                    if (fresh is None or fresh.typed_input_digest != artifact.typed_input_digest
                            or fresh.typed_input_ref != artifact.typed_input_ref
                            or fresh.model_dump(mode="json") != original_task_row):
                        raise BoardError("repository_source_replay_changed", "Original Task artifact changed", status_code=409)
                    await db.commit()
            return BoardMutation(existing, event, idempotent_replay=True)
        await self.repository._validate_goal(db, owner, goal_id=request.input.goal_ref,
            goal_revision=request.goal_revision)
        await self.strategy(owner, request.input.goal_ref)
        evidence = await self.evidence(db, owner, request.input.evidence_refs)
        if request.plan is None:
            if self.planner is None:
                raise BoardError("general_task_planner_inactive", "Restore the governed task planner", status_code=503)
            descriptors, tool_digest = self.snapshot()
            if request.input.tool_set_digest is not None and request.input.tool_set_digest != tool_digest:
                raise BoardError("general_task_tool_set_changed", "Refresh the current tool contract", status_code=409)
            task_input = request.input.model_copy(update={"tool_set_digest": tool_digest})
            try:
                proposal = await self.planner.propose(db, owner, task_input, descriptors,
                    goal_revision=request.goal_revision, idempotency_key=request.idempotency_key,
                    with_provenance=True)
                from src.work_board.general_task_planner import TaskProposalResult
                if not isinstance(proposal, TaskProposalResult):
                    raise BoardError("general_task_provenance_missing", "Planner must preserve its original accounting provenance", status_code=409)
                if proposal.plan is None:
                    envelope = GeneralTaskEnvelope(task_input=task_input,
                        proposal_error=proposal.error, strategy=await self.strategy(owner, task_input.goal_ref),
                        proposal_group=proposal.group, proposal_provenance=proposal.provenance)
                else:
                    plan = proposal.plan
                    request = GeneralTaskCreate(goal_revision=request.goal_revision,
                        idempotency_key=request.idempotency_key, input=task_input, plan=plan,
                        expected_plan_revision=plan.revision)
                    envelope = await self.validate(owner, request)
                    envelope = envelope.model_copy(update={"proposal_group": proposal.group,
                        "proposal_provenance": proposal.provenance})
            except BoardError as exc:
                if exc.code != "general_task_plan_invalid":
                    raise
                # Invalid proposal output is data only. Keep an editable card,
                # retaining no unsafe/untyped executable fields from the model.
                envelope = GeneralTaskEnvelope(task_input=task_input,
                    proposal_error="general_task_plan_invalid",
                    strategy=await self.strategy(owner, task_input.goal_ref),
                    proposal_group=proposal.group, proposal_provenance=proposal.provenance)
        else:
            envelope = await self.validate(owner, request)
            from src.auth.service import authenticate_session
            from src.work_board.general_task_proposal import new_group
            operator = await authenticate_session(owner.session_id, touch=False)
            if operator.principal.principal_id != owner.principal_id:
                raise BoardError("general_task_owner_changed", "Original operator changed", status_code=403)
            descriptors, _ = self.snapshot()
            group = new_group(owner, envelope.task_input, descriptors,
                goal_revision=request.goal_revision, request_key=request.idempotency_key,
                expires_at=min(operator.idle_expires_at, operator.absolute_expires_at))
            envelope = envelope.model_copy(update={"proposal_group": group})
        envelope = envelope.model_copy(update={"evidence": evidence})
        repository_source_check = repository_source_scope = None
        if envelope.plan is not None and any(step.tool_id == "repository_work" for step in envelope.plan.steps):
            reject_repository_scope(envelope.plan)
            from src.workflows.repo_repair_source import prepare_repository_task_publication
            binding, repository_source_check, repository_source_scope = prepare_repository_task_publication(
                self.repository_source_service, envelope, owner=owner, goal_revision=request.goal_revision)
            envelope = GeneralTaskEnvelope.model_validate({**envelope.model_dump(mode="json"),
                "repository_source": binding.model_dump(mode="json")})
        if envelope.task_input.document_source is not None:
            from src.work_board.document_preparation import resolve
            await resolve(db, owner, envelope.task_input.document_source, goal_id=envelope.task_input.goal_ref)
        from src.work_board.general_task_proposal import seal_proposal_publication
        publication = await seal_proposal_publication(db, owner, envelope, goal_revision=request.goal_revision)
        artifact = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(
            schema_version=1, capability_id=CAPABILITY, goal_id=request.input.goal_ref,
            goal_revision=request.goal_revision, input=envelope.model_dump(mode="json"),
            idempotency_key="general:" + request.idempotency_key), general_task_publication=publication)
        # prepare_input_artifact reserves durably before filesystem I/O; task
        # publication binds that exact artifact under the repository writer CAS.
        from contextlib import AsyncExitStack
        async with AsyncExitStack() as scopes:
            if repository_source_scope is not None:
                await scopes.enter_async_context(repository_source_scope())
            if publication_authority_scope is not None:
                await scopes.enter_async_context(publication_authority_scope())
            async def checked_publication(current_db):
                if repository_source_check is not None:
                    await repository_source_check(current_db)
                if publication_authority_check is not None:
                    await publication_authority_check(current_db)
            mutation = await self.repository.create_task(db, owner, WorkBoardTaskCreate(
                title=request.input.intent[:200], body="General registered-tool task",
                goal_id=request.input.goal_ref, goal_revision=request.goal_revision,
                capability_id=CAPABILITY, input_artifact_id=artifact.artifact_id,
                status=WorkBoardStatus.todo if request.accept else WorkBoardStatus.triage,
                idempotency_scope="general-task", idempotency_key=request.idempotency_key,
                requires_review=True), publication_authority_check=(checked_publication
                    if repository_source_check is not None or publication_authority_check is not None else None))
            if publication_authority_scope is not None or repository_source_scope is not None:
                # The original publication CAS commits before its canonical
                # configuration fence is released. Files were staged earlier.
                await db.commit()
            return mutation

    async def plan(self, db, owner, task_id):
        from src.work_board.dispatcher import _parse_typed_input
        from sqlalchemy import select
        from src.db.models import WorkBoardEvent
        task = await self.repository.get_task(db, owner, task_id)
        if task.capability_id != CAPABILITY:
            raise BoardError("general_task_unavailable", "General task unavailable", status_code=404)
        envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(task))
        acceptance_events = (await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.task_id == task_id, WorkBoardEvent.owner_principal_id == owner.principal_id,
            WorkBoardEvent.owner_session_id == owner.session_id,
            WorkBoardEvent.kind.in_(("task.created", "task.promote"))))).scalars().all()
        accepted = any(json.loads(item.metadata_json).get("status") == "todo" for item in acceptance_events)
        payload = {"task_id": task.task_id, "task_revision": task.task_revision,
            "accepted": accepted,
            **envelope.model_dump(mode="json", exclude={"repository_source"}), "no_learning": True,
            "approval_pause": await self.approval_pause(db, owner, task, envelope)}
        from src.db.models import WorkBoardAttempt, WorkflowRunState
        from src.workflows.general_task_guard import read_manifest
        from src.work_board.general_task_runtime_artifacts import verify_readonly_native_projection, read_native_artifact_reference
        from src.work_board.general_task_native import current_plan
        from src.work_board.contracts import GeneralTaskArtifactRef
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id)
            .order_by(WorkBoardAttempt.started_at.desc()).limit(1))
        parent = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == attempt.workflow_run_id)) if attempt and attempt.workflow_run_id else None
        manifest = read_manifest(parent) if parent else None
        if manifest is not None:
            await verify_readonly_native_projection(db, owner, parent, task, attempt, manifest)
            plan = current_plan(manifest, envelope)
            payload["plan"] = plan.model_dump(mode="json")
            steps = []
            for index, step_id in enumerate(manifest.step_ids):
                receipt = read_native_artifact_reference(GeneralTaskArtifactRef(
                    artifact_id=manifest.step_receipt_artifact_ids[index],
                    digest=manifest.step_receipt_digests[index], schema_version="StepReceipt.v1"),
                    parent_job_id=parent.run_identity, creation_digest=manifest.creation_digest)
                steps.append({"step_id": step_id, "status": receipt.status, "contact_state": receipt.contact_state,
                    "invocation_id": receipt.invocation_id, "plan_revision": receipt.plan_revision,
                    "artifact_refs": [ref.model_dump(mode="json") for ref in receipt.artifact_refs]})
            display_phase = manifest.phase
            if parent.status == "succeeded" and all(any(item["step_id"] == step.step_id
                and item["status"] == "verified" and item["contact_state"] == "settled" for item in steps)
                for step in plan.steps):
                from src.workflows.job_runtime import durable_job_repository
                projection = await durable_job_repository.get_job(parent.run_identity)
                final_outputs, final_artifacts = self.recovered_outputs(projection,
                    envelope.model_copy(update={"plan": plan}))
                final_artifact = final_artifacts.get(plan.steps[-1].step_id)
                if (len(final_outputs) == len(plan.steps) and final_artifact is not None
                    and any(effect.get("effect_type") == "board_child_readback"
                        and effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded"
                        and effect.get("content_sha256") == final_artifact["content_sha256"]
                        and effect.get("details", {}).get("verified") is True for effect in projection["effects"])):
                    display_phase = "complete"
            payload["native_execution"] = {"phase": display_phase, "plan_revision": manifest.plan_revision,
                "manifest_revision": manifest.manifest_revision, "original_deadline_at": manifest.original_deadline_at.isoformat(),
                "native_deadline_at": manifest.native_deadline_at.isoformat(), "steps": steps,
                "admitted_invocation_ids": list(manifest.admitted_invocation_ids),
                "remaining_steps": [step.step_id for step in plan.steps if not any(
                    item["step_id"] == step.step_id and item["status"] == "verified" for item in steps)],
                "partial_output_refs": [ref for item in steps if item["status"] == "verified" for ref in item["artifact_refs"]],
                "no_learning": True}
            if attempt.cancel_requested_at is not None:
                from src.workflows.general_task_guard import read_general_task_native_cancel
                from src.workflows.job_runtime import DurableJobError
                try:
                    cancellation = read_general_task_native_cancel(parent, task, attempt)
                except (BoardError, DurableJobError):
                    cancellation = {"state": "pending", "child_ids": list(manifest.admitted_invocation_ids),
                        "callback_closed": False, "effect_debt": True,
                        "reason": "general_task_native_cancel_evidence_unavailable"}
                    payload["native_execution"]["phase"] = "unknown_recovery"
                payload["native_execution"]["cancellation"] = cancellation
        return payload

    def recovered_outputs(self, projection, envelope):
        """Adopt only physically verified completed outputs, never call intent."""
        from src.work_board.input_artifacts import _safe_file_bytes
        from src.workspace import canonical_workspace_root
        from config.settings import settings
        outputs, artifacts = {}, {}
        checkpoints = projection.get("checkpoints", [])
        for step in envelope.plan.steps:
            matches = [item for item in checkpoints if item.get("checkpoint_id") == "general:verified:" + step.step_id]
            if not matches:
                continue
            if len(matches) != 1:
                raise BoardError("general_task_output_changed", "Completed output needs reconciliation", status_code=409)
            artifact = matches[0].get("payload")
            if isinstance(artifact, dict) and artifact.get("schema_version") == "general_task.checkpoint_reservation.v1":
                from types import SimpleNamespace
                from src.workflows.general_task_guard import read_native_checkpoint_reservation
                from src.workflows.job_runtime import DurableJobError
                try:
                    reservation = read_native_checkpoint_reservation(SimpleNamespace(
                        run_identity=projection.get("job_id"), checkpoint_receipts_json=json.dumps(checkpoints)),
                        "general:verified:" + step.step_id)
                    if reservation.invocation_id is None or reservation.binding_digest is None:
                        raise ValueError("output reservation needs original child binding")
                except (ValueError, DurableJobError) as exc:
                    raise BoardError("general_task_output_changed", "Output reservation changed", status_code=409) from exc
                continue
            if not isinstance(artifact, dict) or artifact.get("producer_ref") != projection.get("job_id") or artifact.get("step_id") != step.step_id or artifact.get("plan_digest") != digest(envelope.model_dump(mode="json")):
                raise BoardError("general_task_output_changed", "Completed output binding changed", status_code=409)
            reference = artifact.get("file_path", "")
            if not reference.startswith("artifacts/work-board/general-tasks/") or ".." in reference:
                raise BoardError("general_task_output_changed", "Completed output path changed", status_code=409)
            raw = _safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / reference,
                expected_digest=artifact["content_sha256"], expected_size=artifact["size_bytes"])
            body = json.loads(raw)
            if body.get("step_id") != step.step_id:
                raise BoardError("general_task_output_changed", "Completed output identity changed", status_code=409)
            output = body["output"]
            descriptor = next(item for item in envelope.descriptors if item.tool_id == step.tool_id)
            validate_schema(descriptor.output_schema, output)
            validate_schema(step.output_contract, output)
            if not any(effect.get("effect_type") == "general_tool_call" and effect.get("status") == "succeeded" and effect.get("details", {}).get("step_id") == step.step_id and effect.get("details", {}).get("output_exists") is True for effect in projection.get("effects", [])):
                raise BoardError("general_task_output_changed", "Completed tool readback missing", status_code=409)
            outputs[step.step_id], artifacts[step.step_id] = output, artifact
        return outputs, artifacts

    async def approval_pause(self, db, owner, task, envelope):
        from sqlalchemy import select
        from src.db.models import WorkBoardAttempt, ApprovalRequest, WorkflowRunState
        from src.workflows.job_runtime import durable_job_repository
        from src.work_board.contracts import GeneralTaskResume
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id)
            .order_by(WorkBoardAttempt.started_at.desc()).limit(1))
        if attempt is None or attempt.ended_at is not None or not attempt.workflow_run_id:
            return None
        projection = await durable_job_repository.get_job(attempt.workflow_run_id)
        if projection.get("status") != "paused" or projection.get("failure_reason") != "general_task_approval_required":
            return None
        parent = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == attempt.workflow_run_id).execution_options(populate_existing=True))
        from src.workflows.general_task_guard import read_manifest
        manifest = read_manifest(parent) if parent else None
        if manifest is not None:
            if manifest.phase != "approval_wait":
                return None
            from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
            from src.work_board.contracts import GeneralTaskArtifactRef
            from src.workflows.general_task_guard import verify_native_approval_transition, child_binding, _current, _assert_joint_manifest
            awaiting = []
            for index in range(len(manifest.step_ids)):
                receipt = read_native_artifact_reference(GeneralTaskArtifactRef(
                    artifact_id=manifest.step_receipt_artifact_ids[index],
                    digest=manifest.step_receipt_digests[index], schema_version="StepReceipt.v1"),
                    parent_job_id=parent.run_identity, creation_digest=manifest.creation_digest)
                if receipt.status == "awaiting_approval":
                    awaiting.append(receipt)
            if len(awaiting) != 1:
                raise BoardError("general_task_resume_binding_changed", "Exact native approval wait required", status_code=409)
            receipt = awaiting[0]
            child = await durable_job_repository._fetch(db, receipt.child_job_id)
            reason, status = None, "unavailable"
            try:
                original = child_binding(child)
                current_parent, current_task, current_attempt, current_manifest, _ = await _current(
                    durable_job_repository, db, parent.run_identity)
                _assert_joint_manifest(current_parent, current_task, current_attempt, current_manifest)
                witness, approval = await verify_native_approval_transition(db, child, current_parent)
                status = approval.status
                if (owner.principal_id != original.owner_principal_id or owner.session_id != original.original_root_id
                    or witness.phase != "approval_wait" or approval.status != "approved"):
                    reason = "general_task_approval_not_approved"
                else:
                    await self.validate_native_resume(db, owner, current_task, current_attempt,
                        current_parent, current_manifest, envelope, child, original,
                        GeneralTaskResume(expected_revision=current_task.task_revision,
                            expected_plan_revision=current_manifest.plan_revision,
                            workflow_run_id=current_parent.run_identity, attempt_id=current_attempt.attempt_id,
                            fencing_token=current_attempt.fencing_token, workflow_revision=current_parent.revision,
                            approval_id=receipt.approval_id, child_job_id=child.run_identity,
                            expected_manifest_revision=current_manifest.manifest_revision))
            except Exception as exc:
                reason = getattr(exc, "code", "general_task_resume_unavailable")
            return {"approval_id": receipt.approval_id, "approval_status": status,
                "step_id": receipt.step_id, "tool_id": json.loads(child.arguments_json)["tool_id"],
                "workflow_run_id": parent.run_identity, "attempt_id": attempt.attempt_id,
                "fencing_token": attempt.fencing_token, "workflow_revision": parent.revision,
                "child_job_id": child.run_identity, "expected_manifest_revision": manifest.manifest_revision,
                "expected_plan_revision": manifest.plan_revision,
                "original_deadline_at": projection.get("deadline_at"), "can_resume": reason is None,
                "reason": reason}
        waits = [item.get("payload") for item in projection.get("checkpoints", [])
            if isinstance(item.get("payload"), dict) and item["payload"].get("phase") == "approval_precontact"]
        if len(waits) != 1:
            return None
        wait = waits[0]
        approval = await db.get(ApprovalRequest, wait["approval_id"])
        status = str(approval.status) if approval else "unavailable"
        if approval and (not approval.expires_at or approval.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)):
            status = "expired"
        body = GeneralTaskResume(expected_revision=task.task_revision,
            expected_plan_revision=envelope.plan.revision, workflow_run_id=attempt.workflow_run_id,
            attempt_id=attempt.attempt_id, fencing_token=attempt.fencing_token,
            workflow_revision=projection["revision"], approval_id=wait["approval_id"])
        reason = None
        try:
            await self.validate_resume(db, owner, task.task_id, body, projection)
        except Exception as exc:
            reason = getattr(exc, "code", "general_task_resume_unavailable")
        return {"approval_id": wait["approval_id"], "approval_status": status,
            "step_id": wait["step_id"], "tool_id": wait["tool_id"],
            "workflow_run_id": attempt.workflow_run_id, "attempt_id": attempt.attempt_id,
            "fencing_token": attempt.fencing_token, "workflow_revision": projection["revision"],
            "original_deadline_at": projection.get("deadline_at"), "can_resume": reason is None,
            "reason": reason}

    async def validate_resume(self, db, owner, task_id, request, projection):
        from sqlalchemy import select
        from src.db.models import WorkBoardAttempt, ApprovalRequest
        from src.auth.service import authenticate_session
        from src.work_board.dispatcher import _parse_typed_input, _safe_digest, WorkBoardDispatcher
        operator = await authenticate_session(owner.session_id, touch=False)
        if operator.principal.principal_id != owner.principal_id:
            raise BoardError("general_task_owner_changed", "Current owner authorization is required", status_code=409)
        task = await self.repository.get_task(db, owner, task_id)
        attempt = await db.get(WorkBoardAttempt, request.attempt_id, populate_existing=True)
        if task.task_revision != request.expected_revision or task.status is not WorkBoardStatus.blocked or task.block_reason != "awaiting_approval" or task.capability_id != CAPABILITY:
            raise BoardError("stale_revision", "Approval-wait task changed", status_code=409)
        envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(task))
        if envelope.plan.revision != request.expected_plan_revision:
            raise BoardError("stale_revision", "Reviewed plan changed", status_code=409)
        lease = projection.get("lease") or {}
        if (attempt is None or attempt.task_id != task_id or attempt.ended_at is not None or attempt.cancel_requested_at is not None or attempt.workflow_run_id != request.workflow_run_id or attempt.fencing_token != request.fencing_token or attempt.lease_owner is not None
            or projection.get("job_id") != request.workflow_run_id or projection.get("status") != "paused" or projection.get("failure_reason") != "general_task_approval_required" or projection.get("revision") != request.workflow_revision or lease.get("fencing_token") != request.fencing_token
            or projection.get("owner", {}).get("principal_id") != owner.principal_id or projection.get("operator_session_id") != owner.session_id or projection.get("session_id") != owner.session_id or projection.get("goal_id") != task.goal_id or projection.get("goal_revision") != task.goal_revision or projection.get("job_kind") != CAPABILITY
            or projection.get("owner", {}).get("kind") != "user" or projection.get("owner", {}).get("service_id") is not None or projection.get("capability_version") != "1"
            or projection.get("root_run_identity") != request.workflow_run_id or projection.get("parent_run_identity") is not None):
            raise BoardError("general_task_resume_binding_changed", "Exact original task attempt is required", status_code=409)
        expected_inputs = {"task_id": task.task_id, "attempt_id": attempt.attempt_id,
            "capability_id": task.capability_id, "typed_input_ref": task.typed_input_ref,
            "typed_input_digest": task.typed_input_digest, **_parse_typed_input(task)}
        handoffs = WorkBoardDispatcher._attempt_parent_handoffs(attempt)
        if handoffs:
            expected_inputs.update(parent_handoff_context=handoffs, parent_handoff_digest=attempt.parent_handoff_digest)
        if (projection.get("input_digest") != _safe_digest(expected_inputs)
            or projection.get("idempotency", {}).get("key") != f"{task.task_id}:{attempt.attempt_id}"
            or projection.get("idempotency", {}).get("scope") != "work-board-attempt"
            or projection.get("authority_digest") != _safe_digest(projection.get("declared_authority", {}))):
            raise BoardError("general_task_resume_binding_changed", "Original immutable execution binding changed", status_code=409)
        deadline = datetime.fromisoformat(str(projection["deadline_at"]).replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
        if deadline <= datetime.now(timezone.utc):
            raise BoardError("general_task_deadline", "Original execution deadline expired", status_code=409)
        await self.repository._validate_goal(db, owner, goal_id=task.goal_id, goal_revision=task.goal_revision)
        await self.recheck_authority(db, owner, envelope)
        outputs, _artifacts = self.recovered_outputs(projection, envelope)
        if any(item.get("status") in {"unknown", "intent", "dispatched"} for item in projection.get("effects", [])):
            raise BoardError("general_task_unresolved_step", "Unknown contacted work requires reconciliation", status_code=409)
        waits = [item.get("payload") for item in projection.get("checkpoints", [])
            if isinstance(item.get("payload"), dict) and item["payload"].get("phase") == "approval_precontact"]
        if len(waits) != 1:
            raise BoardError("general_task_resume_binding_changed", "Exact no-contact proof is required", status_code=409)
        wait = waits[0]
        step = next((item for item in envelope.plan.steps if item.step_id == wait.get("step_id")), None)
        if step is None or step.step_id in outputs or not set(step.depends_on) <= outputs.keys():
            raise BoardError("general_task_resume_binding_changed", "Pending step binding changed", status_code=409)
        descriptor = next(item for item in envelope.descriptors if item.tool_id == step.tool_id)
        inputs = resolve_input(step.input, outputs)
        approval = await db.get(ApprovalRequest, request.approval_id, populate_existing=True)
        metadata = self.registry.approval_context(descriptor, inputs, job_id=request.workflow_run_id)
        details = json.loads(approval.details_json or "{}") if approval else {}
        if (approval is None or approval.status != "approved" or not approval.expires_at or approval.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
            or approval.owner_principal_id != owner.principal_id or approval.operator_session_id != owner.session_id or approval.session_id != owner.session_id
            or approval.tool_name != metadata["tool_name"] or approval.fingerprint != metadata["fingerprint"] or details.get("approval_context") != metadata["approval_context"]
            or details.get("general_task_wait_binding") != wait
            or wait.get("approval_id") != request.approval_id or wait.get("job_id") != request.workflow_run_id or wait.get("fence") != request.fencing_token
            or wait.get("input_digest") != digest(inputs) or wait.get("descriptor_digest") != digest(descriptor.model_dump(mode="json")) or wait.get("fingerprint") != approval.fingerprint
            or wait.get("authority_digest") != projection.get("authority_digest") or wait.get("deadline_at") != projection.get("deadline_at")
            or wait.get("plan_digest") != digest(envelope.model_dump(mode="json"))):
            raise BoardError("approval_not_current", "Approve the exact current unexpired tool request", status_code=409)
        if not any(item.get("effect_id") == wait.get("effect_id") and item.get("status") == "succeeded" and item.get("details", {}).get("never_contacted") is True and item.get("content_sha256") == digest(wait) for item in projection.get("effects", [])):
            raise BoardError("general_task_unresolved_step", "Positive no-contact proof is required", status_code=409)
        return task, attempt, envelope

    async def validate_native_resume(self, db, owner, task, attempt, parent, manifest, envelope, child, binding, request):
        """Preflight the exact waiting child without consuming its approval."""
        from src.workflows.general_task_guard import verify_native_approval_transition
        from src.work_board.general_task_native import current_plan
        from src.work_board.general_task_runtime_artifacts import read_bound_native_tool_input, read_current_native_outputs
        transition, approval = await verify_native_approval_transition(db, child, parent)
        if (transition.phase != "approval_wait" or transition.approval_id != request.approval_id
            or approval.status != "approved" or binding.parent_job_id != parent.run_identity
            or binding.task_id != task.task_id or binding.attempt_id != attempt.attempt_id
            or owner.principal_id != task.owner_principal_id or owner.session_id != task.owner_session_id):
            raise BoardError("general_task_resume_binding_changed", "Exact original approved native child required", status_code=409)
        plan = current_plan(manifest, envelope)
        await self.recheck_authority(db, owner, envelope.model_copy(update={"plan": plan}))
        step = next((item for item in plan.steps if item.step_id == binding.step_id), None)
        if step is None:
            raise BoardError("general_task_resume_binding_changed", "Original pending plan step required", status_code=409)
        await read_current_native_outputs(db, parent, task, attempt, manifest, envelope, step.depends_on)
        private = read_bound_native_tool_input(child, binding)
        descriptor = next((item for item in self.registry.descriptors() if item.tool_id == private.tool_id), None)
        if descriptor is None or digest(descriptor.model_dump(mode="json")) != binding.descriptor_digest:
            raise BoardError("general_task_tool_contract_changed", "Original registered descriptor required", status_code=409)
        metadata = self.registry.approval_context(descriptor, private.inputs, job_id=child.run_identity)
        if (metadata["fingerprint"] != transition.approval_fingerprint
            or digest(metadata["approval_context"]) != transition.approval_context_digest
            or metadata["tool_name"] != approval.tool_name):
            raise BoardError("approval_not_current", "Approve the exact current native tool request", status_code=409)

    async def update_plan(self, db, owner, task_id, request):
        from sqlalchemy import select
        from src.db.models import WorkBoardAttempt
        from src.work_board.dispatcher import _parse_typed_input
        from src.work_board.input_artifacts import (
            prepare_input_artifact, stage_input_artifact, recheck_staged_input,
            resolve_input_artifact_for_task, bind_input_artifact,
            InputRetirementWitness, _metadata_digest, _revoke_input_artifact_locked,
        )
        from src.work_board.repository import _begin_sqlite_immediate, BoardRevisionConflict
        from src.work_board.pipelines import root_binding
        task = await self.repository.get_task(db, owner, task_id)
        if task.capability_id != CAPABILITY or task.status != WorkBoardStatus.triage:
            raise BoardError("general_task_plan_locked", "Only inert Triage plans are editable", status_code=409)
        if task.task_revision != request.expected_revision:
            raise BoardRevisionConflict(task_id, request.expected_revision, task.task_revision)
        prior = GeneralTaskEnvelope.model_validate(_parse_typed_input(task))
        if (prior.repository_source is not None
                or any(step.tool_id == "repository_work" for step in request.plan.steps)):
            raise BoardError("repository_source_plan_immutable",
                "Use an explicit new source-inspected request; this Task's original source and budget stay bound", status_code=409)
        if (prior.plan.revision if prior.plan else 0) != request.expected_plan_revision:
            raise BoardError("general_task_plan_revision_stale", "Plan changed before editing", status_code=409)
        original = await resolve_input_artifact_for_task(db, owner, artifact_id=task.input_artifact_id,
            goal_id=task.goal_id, goal_revision=task.goal_revision, capability_id=CAPABILITY,
            expected_task_id=task_id)
        retirement = InputRetirementWitness(original.row.artifact_id, original.row.revision,
            _metadata_digest(original.row), task_id, task.task_revision, task.typed_input_ref,
            original.row.payload_sha256, original.row.size_bytes, canonical(root_binding()))
        _, tool_digest = self.snapshot()
        revised_request = GeneralTaskCreate(goal_revision=task.goal_revision,
            idempotency_key=request.idempotency_key,
            input=prior.task_input.model_copy(update={"tool_set_digest": tool_digest}),
            plan=request.plan, expected_plan_revision=request.plan.revision)
        revised = await self.validate(owner, revised_request)
        revised = revised.model_copy(update={"evidence": await self.evidence(db, owner, prior.task_input.evidence_refs),
            "proposal_group": prior.proposal_group, "proposal_provenance": prior.proposal_provenance})
        from src.work_board.general_task_proposal import seal_proposal_publication
        publication = await seal_proposal_publication(db, owner, revised, goal_revision=task.goal_revision)
        metadata = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(
            schema_version=1, capability_id=CAPABILITY, goal_id=task.goal_id,
            goal_revision=task.goal_revision, input=revised.model_dump(mode="json"),
            idempotency_key="general-edit:" + request.idempotency_key), general_task_publication=publication)
        staged = await stage_input_artifact(db, owner, artifact_id=metadata.artifact_id,
            capability_id=CAPABILITY, goal_id=task.goal_id, goal_revision=task.goal_revision)
        await _begin_sqlite_immediate(db)
        current = await self.repository.get_task(db, owner, task_id)
        await db.refresh(current)
        if current.task_revision != request.expected_revision or current.status != WorkBoardStatus.triage:
            raise BoardError("general_task_plan_revision_stale", "Task changed before plan binding", status_code=409)
        if await db.scalar(select(WorkBoardAttempt.attempt_id).where(WorkBoardAttempt.task_id == task_id).limit(1)):
            raise BoardError("general_task_plan_locked", "Attempt history locks the immutable plan", status_code=409)
        await self.repository._validate_goal(db, owner, goal_id=current.goal_id, goal_revision=current.goal_revision)
        self.recheck(revised)
        binding_request = WorkBoardTaskCreate(title=current.title, goal_id=current.goal_id,
            goal_revision=current.goal_revision, capability_id=CAPABILITY,
            input_artifact_id=metadata.artifact_id, idempotency_key=request.idempotency_key)
        resolved = await recheck_staged_input(db, owner, binding_request, witness=staged)
        # Reuse the existing input-retirement CAS. Old bytes remain private,
        # immutable historical evidence and the revoked row cannot execute.
        await _revoke_input_artifact_locked(db, owner, witness=retirement)
        await self.repository._cas_task_update(db, owner, current,
            expected_revision=request.expected_revision,
            values={"input_artifact_id": metadata.artifact_id,
                "typed_input_ref": metadata.typed_input_ref, "typed_input_digest": metadata.typed_input_digest,
                "task_revision": request.expected_revision + 1, "updated_at": datetime.now(timezone.utc)})
        await bind_input_artifact(db, owner, artifact=resolved,
            task_id=task_id, task_revision=current.task_revision)
        await self.repository._event(db, current, owner, kind="task.plan_revised",
            metadata={"plan_revision": revised.plan.revision, "task_revision": current.task_revision})
        return current

    def recheck(self, envelope: GeneralTaskEnvelope):
        if envelope.plan is None or envelope.proposal_error:
            raise BoardError("general_task_plan_incomplete", "Edit and save a valid plan before acceptance", status_code=409)
        current, _ = self.snapshot()
        by_id = {item.tool_id: item for item in current}
        for prior in envelope.descriptors:
            if prior != by_id.get(prior.tool_id):
                raise BoardError("general_task_tool_contract_changed", "Restore or revise the tool contract", status_code=409)
        if len(envelope.plan.steps) > envelope.task_input.limits.max_steps:
            raise BoardError("general_task_plan_invalid", "Plan exceeds its finite task allowance", status_code=422)
        try:
            from src.work_board.general_task_schema import schema_accepts_output
            validate_schema(envelope.task_input.requested_output, check_value=False)
            for step in envelope.plan.steps:
                descriptor = by_id.get(step.tool_id)
                if descriptor is None or descriptor not in envelope.descriptors:
                    raise ValueError("step descriptor is unavailable")
                validate_schema(step.output_contract, check_value=False)
                if not schema_accepts_output(descriptor.output_schema, step.output_contract):
                    raise ValueError("step output contract is incompatible")
                validate_data(step.input, dependencies=set(step.depends_on))
                if not has_pointer(step.input):
                    validate_schema(descriptor.input_schema, step.input)
            final = envelope.plan.steps[-1]
            if not schema_accepts_output(final.output_contract, envelope.task_input.requested_output):
                raise ValueError("requested output contract is incompatible")
        except Exception as exc:
            raise BoardError("general_task_plan_invalid", "Plan violates the exact registered tool schema", status_code=422) from exc

    async def validate_acceptance(self, db, owner, task_id, expected_revision):
        from src.work_board.dispatcher import _parse_typed_input
        from src.work_board.repository import BoardRevisionConflict
        task = await self.repository.get_task(db, owner, task_id)
        if task.task_revision != expected_revision:
            raise BoardRevisionConflict(task_id, expected_revision, task.task_revision)
        if task.status != WorkBoardStatus.triage:
            raise BoardError("general_task_acceptance_state", "Accept the exact inert Triage proposal", status_code=409)
        envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(task))
        await self.recheck_authority(db, owner, envelope)

    async def recheck_authority(self, db, owner, envelope):
        self.recheck(envelope)
        from src.work_board.document_preparation import check_envelope, resolve
        check_envelope(envelope)
        if envelope.task_input.document_source is not None:
            await resolve(db, owner, envelope.task_input.document_source, goal_id=envelope.task_input.goal_ref)
        binding = await self.strategy(owner, envelope.task_input.goal_ref)
        if binding != envelope.strategy:
            raise BoardError("general_task_strategy_changed", "Review the current task method", status_code=409)
        evidence = await self.evidence(db, owner, envelope.task_input.evidence_refs)
        if evidence != envelope.evidence:
            raise BoardError("general_task_evidence_changed", "Review changed evidence", status_code=409)

    async def execute(self, jobs, *, job_id, owner, fence, envelope, principal, resume_child=None):
        if not self.started:
            raise BoardError("general_task_inactive", "Task service is inactive", status_code=503)
        projection = await jobs.get_job(job_id)
        if any(str(item.get("checkpoint_id", "")).startswith("general:step:")
            for item in projection.get("checkpoints", [])):
            raise BoardError("general_task_unresolved_step", "Existing root step intent requires reconciliation", status_code=409)
        from src.work_board.general_task_native import execute_interpreter
        return await execute_interpreter(self, jobs, job_id=job_id, owner=owner,
            fence=fence, principal=principal, resume_child=resume_child)
