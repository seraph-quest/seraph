"""Original live discovery producer and its one-use protected START writer.

Physical input witnesses, job locators and copied scopes are never Sources.
No inference/result continuation is issued here.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import event, update

from .dispatch import NativeServiceBlocked

START_PREFIX = "runtime-service-invocation:programme-start:"
PROGRAMME_METHODS = frozenset({"authority.resolve", "tasks.inspect",
    "research.executeAccepted", "source-extraction.extract", "inference.request"})
_SEAL = object()


class _ProgrammeSource:
    def __init__(self, service, job_id, seal):
        if seal is not _SEAL:
            raise NativeServiceBlocked("native_programme_source_unavailable")
        from src.memory.header_bounds import HeaderReadBudget
        self.service, self.jobs, self.job_id = service, service.jobs, job_id
        self.task, self.loop = asyncio.current_task(), asyncio.get_running_loop()
        self.host = service._lifecycle_host
        self.boot = self.host.boot_nonce
        self.reviewed, self.dispatcher = self.host.reviewed, self.host.service_dispatch
        self.header_budget = HeaderReadBudget()
        self.claim = self.scope = self.publication = self.response = None
        self._delivered_response = None
        self._numeric_reservation = None
        self._current_authority = self._current_authority_raw = None
        self.writer = None
        self.state = "reserved"
        self.closed = False

    def validate(self, *, owner=False, called=None):
        from .bridge import CordisHost
        from .dispatch import CalledServiceInvocation
        service = self.service
        if (type(self) is not _ProgrammeSource or self.closed or not service.started
                or service._native_programme_sources.get(self.job_id) is not self
                or self.task not in service._tasks or self.task.done()
                or asyncio.get_running_loop() is not self.loop
                or service.jobs is not self.jobs or service._lifecycle_host is not self.host
                or type(self.host) is not CordisHost or not self.host.admitting
                or self.host.boot_nonce != self.boot or self.host.reviewed is not self.reviewed
                or self.host.service_dispatch is not self.dispatcher
                or self.dispatcher.jobs is not self.jobs
                or service._lifecycle_dispatcher is not self.dispatcher):
            raise NativeServiceBlocked("native_programme_source_changed")
        if owner and asyncio.current_task() is not self.task:
            raise NativeServiceBlocked("native_programme_original_task_changed")
        if called is not None:
            if type(called) is not CalledServiceInvocation:
                raise NativeServiceBlocked("native_programme_original_pending_changed")
            pending = self.host._pending.get(called.parent_request_id)
            if (type(called) is not CalledServiceInvocation or called.original is not self.scope
                    or called.original.native_programme_source is not self
                    or called.method not in PROGRAMME_METHODS or pending is None
                    or pending.future.done() or pending.native_binding is not called
                    or pending.frame["method"] != called.method
                    or pending.frame["deadline_at"] != called.deadline_at
                    or called.host_boot_nonce != self.boot):
                raise NativeServiceBlocked("native_programme_original_pending_changed")
        return self

    async def is_native_programme(self):
        from src.memory.header_bounds import WRS_BY_RUN
        from .ownership import RuntimeCompositionBinding
        self.validate(owner=True)
        async with self.jobs._session(header_budget=self.header_budget) as db:
            await self.header_budget.certify(db, WRS_BY_RUN, (self.job_id,))
            run = await self.jobs._fetch(db, self.job_id)
            if run is None or run.composition_binding_json is None:
                return False
            self.header_budget.debit(len(run.composition_binding_json) * 6 + 2,
                appearance=("programme-source", "binding-decode"))
            binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
            if binding.native_branch != "programme_discovery":
                raise NativeServiceBlocked("native_programme_original_branch_unavailable")
            return True

    def bind_claim(self, claim):
        from dataclasses import replace
        from .dispatch import capture_original_scope
        self.validate(owner=True)
        if self.state != "reserved" or self.claim is not None:
            raise NativeServiceBlocked("native_programme_claim_already_captured")
        scope = capture_original_scope(claim, self.host)
        if (scope.witness["invocation_ref"] != self.job_id
                or scope.binding.native_branch != "programme_discovery"
                or frozenset(scope.binding.allowed_child_methods) != PROGRAMME_METHODS):
            raise NativeServiceBlocked("native_programme_claim_changed")
        self.claim = claim
        self.scope = replace(scope, native_programme_source=self)
        self.state = "claimed"
        return self.scope

    async def prepare_selection(self, db, run):
        """Issue persisted selectors only on this original writer/frame."""
        from src.memory.composition_headers import (certify_composition_superset,
            preflight_programme_identity_component)
        from src.workspace.accounting_witness import CompositionSessionGuard
        self.validate()
        guard = db.info.get("composition_guard")
        if (type(guard) is not CompositionSessionGuard or guard.db is not db
                or guard.header_budget is not self.header_budget or run.run_identity != self.job_id):
            raise NativeServiceBlocked("native_programme_original_writer_changed")
        common = await certify_composition_superset(db, self.header_budget,
            _header_budget=self.header_budget)
        connection = await db.connection()
        def selected(conn):
            self.validate()
            selection = guard._select_programmes(conn, common)
            if self.job_id not in selection.runs:
                raise NativeServiceBlocked("native_programme_original_selection_missing")
            identity = preflight_programme_identity_component(common, original_selection=selection)
            return selection, identity
        selection, identity = await connection.run_sync(selected)
        self.writer = (asyncio.current_task(), db, connection, guard, selection, identity)
        return guard, identity

    def authority_selection(self, db):
        self.validate()
        held = self.writer
        if (held is None or held[0] is not asyncio.current_task() or held[1] is not db
                or db.info.get("composition_guard") is not held[3]
                or held[3].header_budget is not self.header_budget
                or db.sync_session.connection() is not held[4].common33.connection):
            raise NativeServiceBlocked("native_programme_original_writer_changed")
        held[3]._validate_programme_selection(held[4])
        return held[3], held[5]

    async def physical_inputs(self):
        from inspect import signature
        from src.workflows.research_sources import physical_discovery_inputs
        self.validate()
        # Dependency availability is not authority. The actual owner must
        # implement the private Source seam; raw frame forwarding is forbidden.
        if "native_programme_source" not in signature(physical_discovery_inputs).parameters:
            raise NativeServiceBlocked("native_programme_physical_reader_unavailable")
        return await physical_discovery_inputs(self.jobs, self.job_id, native_programme_source=self)

    async def preflight_current(self):
        from src.memory.header_bounds import WRS_BY_RUN
        from src.workflows.research_guard import discovery_writer_scope
        from src.work_board.research_parent import discovery_authority
        from src.guardian.goal_programmes import goal_programme_service
        self.validate()
        async with discovery_writer_scope() as policy:
            async with self.jobs._session(header_budget=self.header_budget) as db:
                await self.header_budget.certify(db, WRS_BY_RUN, (self.job_id,))
                run = await self.jobs._fetch(db, self.job_id)
                guard, identity = await self.prepare_selection(db, run)
                upper = self.writer[4].common33.rows[("workflow_run_states", self.job_id)][1]
                self.header_budget.debit(upper, appearance=("programme-source", "authority-model"))
                authority = discovery_authority(run.declared_authority_json)
                await goal_programme_service.validate_current_binding(db=db, binding=authority.programme_binding,
                    policy=policy, original_admission_guard=guard, identity_certificate=identity,
                    native_programme_source=self)
                self._current_authority, self._current_authority_raw = authority, run.declared_authority_json
        self.writer = None

    async def delivered(self, result):
        from src.workflows.job_runtime import _digest
        self.validate(owner=True)
        proof = self.publication
        if (self.state != "start_committed" or type(proof) is not _StartPublication
                or not proof.committed or not proof.checked or result != proof.response
                or result.get("status") != "succeeded" or result.get("memory_status") != "no_learning"
                or result["value"]["job_ref"] != self.job_id):
            raise NativeServiceBlocked("native_programme_start_response_unavailable")
        # Independently current after actual delivery, before original execution.
        from src.workflows.research_sources import physical_discovery_inputs
        from src.workflows.research_guard import discovery_writer_scope, assert_discovery_authority
        await self.preflight_current()
        witness = await self.physical_inputs()
        async with discovery_writer_scope(witness=witness):
            async with self.jobs._writer_session(header_budget=self.header_budget) as db:
                run = await self.jobs._fetch(db, self.job_id)
                await self.prepare_selection(db, run)
                await assert_discovery_authority(db, run.declared_authority_json, run=run,
                    native_programme_source=self)
                from .ownership import validate_invocation
                await validate_invocation(db, self.scope.binding, header_budget=self.header_budget)
                self.jobs._assert_lease(run, owner=self.scope.witness["lease_owner"],
                    fencing_token=self.scope.witness["fencing_token"])
                self.header_budget.debit(self.writer[4].common33.rows[("workflow_run_states", self.job_id)][1],
                    appearance=("programme-start", "delivered-journal"))
                import json
                matches = [r for r in json.loads(run.checkpoint_receipts_json)
                    if r.get("checkpoint_id") == proof.receipt["checkpoint_id"]]
                if matches != [proof.receipt] or _digest(matches[0]["payload"]) != proof.receipt["state_digest"]:
                    raise NativeServiceBlocked("native_programme_start_receipt_changed")
        self.validate(owner=True)
        self.response = result
        self.state = "executing"

    def close(self):
        self.closed = True
        self.writer = None
        if self.service._native_programme_sources.get(self.job_id) is self:
            self.service._native_programme_sources.pop(self.job_id)


def reserve_programme_source(service, job_id):
    service._ready()
    host = service._lifecycle_host
    if (host is None or service._lifecycle_dispatcher is None
            or asyncio.current_task() not in service._tasks
            or job_id in service._native_programme_sources):
        raise NativeServiceBlocked("native_programme_source_unavailable")
    source = _ProgrammeSource(service, job_id, _SEAL)
    service._native_programme_sources[job_id] = source
    try:
        return source.validate(owner=True)
    except BaseException:
        source.close()
        raise


class _StartPublication:
    def __init__(self, source, called, db, statement, before, after, receipt, response, seal):
        if seal is not _SEAL:
            raise NativeServiceBlocked("native_programme_start_publication_unavailable")
        self.source, self.called, self.db, self.statement = source, called, db, statement
        self.before, self.after, self.receipt, self.response = before, after, receipt, response
        self.task = asyncio.current_task()
        self.checked = self.committed = False


def validate_programme_start_publication(db, statement, run_id, before, after, previous, current):
    """Called only by the existing protected-journal Workspace owner."""
    proof = db.info.get("composition_native_programme_start")
    if type(proof) is not _StartPublication:
        raise NativeServiceBlocked("native_programme_start_publication_unavailable")
    source = proof.source.validate(called=proof.called)
    key = proof.receipt["checkpoint_id"]
    if (source.publication is not proof or source.state != "start_pending"
            or proof.db is not db or proof.statement is not statement
            or proof.task is not asyncio.current_task() or run_id != source.job_id
            or proof.before != before or proof.after != after or proof.checked
            or key in previous or current.get(key) != proof.receipt
            or current.keys() - previous.keys() != {key}
            or any(current.get(k) != v for k, v in previous.items())):
        raise NativeServiceBlocked("native_programme_start_publication_changed")
    proof.checked = True
    return (proof.receipt,)


def original_programme_start_numeric_facts(source, called, run):
    """Closed future START widths, over this owner's already-paid facts only.

    No future receipt, body certificate, digest or fee-list authority is built.
    The original Workspace composer owns the complete continuation inventory.
    """
    from types import MappingProxyType
    from src.workflows.job_runtime import (_discovery_utf8_width,
        _discovery_encoded_upper, _discovery_object_width)
    if type(source) is not _ProgrammeSource:
        raise NativeServiceBlocked("native_programme_source_unavailable")
    source.validate(called=called)
    if (source.state != "claimed" or source.publication is not None
            or source.writer is None or run.run_identity != source.job_id
            or source._current_authority is None
            or run.declared_authority_json != source._current_authority_raw):
        raise NativeServiceBlocked("native_programme_start_numeric_facts_changed")
    source.authority_selection(source.writer[1])
    witness = source.scope.witness
    plan = source._current_authority.plan_ref
    plan_width = _discovery_object_width({"artifact_id": _discovery_encoded_upper(plan.artifact_id),
        "digest": _discovery_encoded_upper(plan.digest), "schema_version": 1})
    payload_width = _discovery_object_width({"schema_version": 1,
        "invocation_ref": _discovery_encoded_upper(source.job_id),
        "original_claim_digest": 66, "composition_binding_digest": 66,
        "host_boot_nonce": _discovery_encoded_upper(source.boot), "plan_ref": plan_width,
        "input_digest": _discovery_encoded_upper(run.input_digest),
        "authority_digest": _discovery_encoded_upper(run.authority_digest),
        "run_fingerprint": _discovery_encoded_upper(run.run_fingerprint),
        "attempt_count": _discovery_encoded_upper(run.attempt_count),
        "lease_owner": _discovery_encoded_upper(run.lease_owner),
        "fencing_token": _discovery_encoded_upper(run.fencing_token),
        "original_deadline_at": _discovery_encoded_upper(witness["original_deadline_at"]),
        "no_learning": 4})
    checkpoint_width = len(START_PREFIX) + _discovery_utf8_width(witness["claim_ref"])
    receipt_width = _discovery_object_width({"checkpoint_id": 2 + checkpoint_width,
        "state_digest": 66, "safe": 4, "payload": payload_width})
    prior_width = _discovery_utf8_width(run.checkpoint_receipts_json)
    # Canonical escaping is at most six bytes per original UTF-8 byte. Include
    # a full receipt and separator without relying on whitespace cancellation.
    future_journal_width = 6 * prior_width + receipt_width + 1
    widths = {name: _discovery_utf8_width(getattr(run, name))
        for name in type(run).model_fields if type(getattr(run, name)) is str}
    widths["checkpoint_receipts_json"] = future_journal_width
    row_upper = source.writer[4].common33.rows[("workflow_run_states", source.job_id)][1]
    return MappingProxyType({"job_id": source.job_id,
        "prior_journal_bytes": prior_width, "claim_payload_bytes": _discovery_encoded_upper(witness),
        "plan_ref_bytes": plan_width, "payload_bytes": payload_width,
        "receipt_bytes": receipt_width, "future_journal_bytes": future_journal_width,
        "future_text_bytes": MappingProxyType(widths), "future_updated_at_bytes": 44,
        "future_revision_bytes": 20, "row_header_bytes": row_upper,
        "actual_receipt_charge": 2 * row_upper + 4096,
        "actual_journal_charge": 3 * row_upper + 4096})


async def write_programme_start(jobs, db, run, called, payload):
    """One protected CAS. Only the original session owner publishes/commits."""
    import json
    from src.db.models import WorkflowRunState
    from src.workflows.job_runtime import (_digest, _canonical, _bounded_checkpoint_receipts,
        _append_parent_fence_condition, _rowcount_is_one)
    from src.workflows.research_guard import assert_discovery_authority
    from .contracts import succeeded
    source = called.original.native_programme_source
    if type(source) is not _ProgrammeSource or source.jobs is not jobs:
        raise NativeServiceBlocked("native_programme_source_unavailable")
    source.validate(called=called)
    if source.state != "claimed" or source.publication is not None:
        raise NativeServiceBlocked("native_programme_start_exhausted")
    guard, _ = source.authority_selection(db)
    # Only this original Workspace owner may enumerate and reserve the entire
    # START/publication/Current/Unknown continuation on the existing frame.
    # A raw budget, caller-supplied fee tuple or prospective receipt is not
    # that proof. The original owner schedule is currently absent, so STOP
    # before constructing a START receipt or issuing its protected CAS.
    reserve = type(guard).__dict__.get("_reserve_original_native_programme_start")
    if not callable(reserve):
        raise NativeServiceBlocked("native_programme_start_capacity_unavailable")
    await reserve(guard, native_programme_source=source, called=called, run=run)
    source.validate(called=called)
    source.authority_selection(db)
    await assert_discovery_authority(db, run.declared_authority_json, run=run, native_programme_source=source)
    witness = source.scope.witness
    from src.work_board.research_parent import discovery_authority
    authority = discovery_authority(run.declared_authority_json)
    if payload["plan_ref"] != authority.plan_ref.artifact_id:
        raise NativeServiceBlocked("native_programme_plan_changed")
    upper = source.writer[4].common33.rows[("workflow_run_states", source.job_id)][1]
    source.header_budget.debit(upper, appearance=("programme-start", "journal-decode"))
    history = json.loads(run.checkpoint_receipts_json)
    checkpoint_id = START_PREFIX + witness["claim_ref"]
    if any(r.get("checkpoint_id", "").startswith(START_PREFIX) for r in history):
        raise NativeServiceBlocked("native_programme_start_exhausted")
    # Full current row plus bounded claim and ArtifactRef fields conservatively
    # cover the exact closed receipt and journal construction before allocation.
    receipt_upper = 2 * upper + 4096
    source.header_budget.debit(receipt_upper, appearance=("programme-start", "receipt-construction"))
    value = {"schema_version": 1, "invocation_ref": source.job_id,
        "original_claim_digest": _digest(dict(witness)),
        "composition_binding_digest": source.scope.binding.binding_digest,
        "host_boot_nonce": source.boot, "plan_ref": authority.plan_ref.model_dump(mode="json"),
        "input_digest": run.input_digest, "authority_digest": run.authority_digest,
        "run_fingerprint": run.run_fingerprint, "attempt_count": run.attempt_count,
        "lease_owner": run.lease_owner, "fencing_token": run.fencing_token,
        "original_deadline_at": witness["original_deadline_at"], "no_learning": True}
    receipt = {"checkpoint_id": checkpoint_id, "state_digest": _digest(value), "safe": True, "payload": value}
    source.header_budget.debit(upper + receipt_upper, appearance=("programme-start", "journal-construction"))
    after = _canonical(_bounded_checkpoint_receipts([*history, receipt]))
    now = datetime.now(timezone.utc)
    conditions = [WorkflowRunState.run_identity == source.job_id, WorkflowRunState.status == "running",
        WorkflowRunState.revision == run.revision, WorkflowRunState.attempt_count == witness["attempt_count"],
        WorkflowRunState.lease_owner == witness["lease_owner"], WorkflowRunState.fencing_token == witness["fencing_token"],
        WorkflowRunState.deadline_at > now, WorkflowRunState.lease_expires_at > now,
        WorkflowRunState.checkpoint_receipts_json == run.checkpoint_receipts_json]
    _append_parent_fence_condition(conditions, run, now=now, writer_db=db)
    statement = update(WorkflowRunState).where(*conditions).execution_options(synchronize_session=False).values(
        checkpoint_receipts_json=after, revision=WorkflowRunState.revision + 1, updated_at=now)
    response = succeeded("research.executeAccepted", {"job_ref": source.job_id,
        "revision": run.revision + 1, "state": "running"})
    proof = _StartPublication(source, called, db, statement, run.checkpoint_receipts_json, after, receipt, response, _SEAL)
    source.publication, source.state = proof, "start_pending"
    db.info["composition_native_programme_start"] = proof
    def committed(session):
        if session is db.sync_session and proof.checked:
            proof.committed = True
            if not source.closed:
                source.state = "start_committed"
    event.listen(db.sync_session, "after_commit", committed, once=True)
    result = await db.execute(statement)
    if not _rowcount_is_one(result):
        raise NativeServiceBlocked("native_programme_start_cas_changed")
    return response


async def dispatch_programme(dispatcher, frame, payload, called):
    from src.workflows.research_sources import physical_discovery_inputs
    from src.workflows.research_guard import discovery_writer_scope
    from .contracts import succeeded, blocked
    source = called.original.native_programme_source
    if type(source) is not _ProgrammeSource or source.jobs is not dispatcher.jobs:
        raise NativeServiceBlocked("native_programme_source_unavailable")
    source.validate(called=called)
    if called.method == "source-extraction.extract":
        return blocked("native_programme_extraction_source_unavailable")
    if called.method == "inference.request":
        return blocked("native_programme_inference_continuation_unavailable")
    if called.method not in {"authority.resolve", "tasks.inspect", "research.executeAccepted"}:
        raise NativeServiceBlocked("native_programme_original_method_changed")
    await source.preflight_current()
    witness = await source.physical_inputs()
    response = None
    async with discovery_writer_scope(witness=witness):
        async with dispatcher.jobs._session(header_budget=source.header_budget) as db:
            run, _, _ = await dispatcher._current_in_db(db, source.job_id, called.method, called.original,
                native_programme_call=called)
            if called.method == "research.executeAccepted":
                response = await dispatcher.jobs.start_programme_service(db, run, called=called, payload=payload)
            elif called.method == "authority.resolve":
                response = succeeded(called.method, {"authority_ref": source.job_id,
                    "revision": run.revision, "mode": "goal-programme"})
            else:
                if payload["job_ref"] != source.job_id:
                    return blocked("native_job_reference_not_bound")
                import json
                upper = source.writer[4].common33.rows[("workflow_run_states", source.job_id)][1]
                source.header_budget.debit(upper, appearance=("programme-inspect", "artifact-inventory"))
                ids = [r["artifact_id"] for r in json.loads(run.artifact_receipts_json) if r.get("artifact_id")]
                if len(ids) > 16:
                    return blocked("native_artifact_inventory_exceeded")
                response = succeeded(called.method, {"job_ref": source.job_id, "revision": run.revision,
                    "state": run.status, "artifact_refs": ids})
    # Returning occurs after the original publisher/commit context exits.
    return response
