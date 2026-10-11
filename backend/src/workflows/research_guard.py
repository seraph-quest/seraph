"""Research-only parent gate; every other child keeps its live-parent fence."""
from __future__ import annotations

import json
from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.orm import aliased

from src.db.models import OperatorSession, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.work_board.research_contracts import CHILD_KIND, PARENT_CAPABILITY, PARENT_KIND, WAIT_CHILDREN, WAIT_SOURCES


async def assert_research_operator_session(db, run, *, now):
    """Pure exact owner-Root predicate inside the current native writer."""
    from src.workflows.job_runtime import DurableJobLeaseError
    if run.operator_session_id != run.session_id or await db.scalar(select(OperatorSession.id).where(
        OperatorSession.id == run.operator_session_id, OperatorSession.principal_id == run.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False), OperatorSession.idle_expires_at > now,
        OperatorSession.absolute_expires_at > now)) is None:
        raise DurableJobLeaseError("research original operator Root session is inactive")


def append_research_parent_gate(conditions, run, *, now):
    """Return True only for the fixed child kind, including malformed denials."""
    if run.job_kind != CHILD_KIND:
        return False
    try:
        authority = json.loads(run.declared_authority_json)
        from src.work_board.pipelines import root_binding
        from src.work_board.pipeline_contracts import digest
        if (run.capability_version != "1" or run.branch_depth != 1
            or authority["research_slot"] not in {0, 1}
            or authority["live_root_digest"] != digest(root_binding())
            or authority["capability_id"] != "work.readonly-research-child.v1"
            or not run.parent_job_id or not run.parent_fencing_token):
            raise ValueError("fixed research lineage unavailable")
        creation_digest = authority["parent_creation_digest"]
        task_id, attempt_id = authority["parent_board_task_id"], authority["parent_board_attempt_id"]
    except (KeyError, TypeError, ValueError):
        conditions.append(false())
        return True
    parent, task, attempt = aliased(WorkflowRunState), aliased(WorkBoardTask), aliased(WorkBoardAttempt)
    original_session = select(OperatorSession.id).where(
        OperatorSession.id == parent.operator_session_id,
        OperatorSession.principal_id == parent.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now).exists()
    checkpoints = func.json_each(parent.checkpoint_receipts_json).table_valued("key", "value").alias()
    creation = select(checkpoints.c.key).where(
        func.json_extract(checkpoints.c.value, "$.checkpoint_id") == "research:creation",
        func.json_extract(checkpoints.c.value, "$.payload.creation_digest") == creation_digest,
        func.json_extract(checkpoints.c.value, "$.payload.creation_job_fence") == run.parent_fencing_token,
        func.json_extract(checkpoints.c.value, "$.payload.board_attempt_id") == attempt_id,
        func.json_extract(checkpoints.c.value, "$.payload.board_task_id") == task_id,
    ).exists()
    phases = func.json_each(parent.checkpoint_receipts_json).table_valued("key", "value").alias()
    phase = func.json_extract(phases.c.value, "$.payload.phase")
    current_phase = select(phases.c.key).where(
        func.json_extract(phases.c.value, "$.checkpoint_id") == "research:phase",
        func.json_extract(phases.c.value, "$.payload.creation_digest") == creation_digest,
        or_(
            and_(parent.status == "paused", parent.failure_reason.in_([WAIT_SOURCES, WAIT_CHILDREN]),
                phase == parent.failure_reason, parent.lease_owner.is_(None), parent.lease_expires_at.is_(None),
                task.status == WorkBoardStatus.blocked, task.block_reason == parent.failure_reason,
                attempt.lease_owner.is_(None), attempt.lease_expires_at.is_(None)),
            and_(parent.status == "running", phase.in_(["research_funding", "research_assembly"]),
                parent.lease_owner.is_not(None), parent.lease_expires_at > now,
                task.status == WorkBoardStatus.running,
                attempt.lease_owner.is_not(None), attempt.lease_expires_at > now),
        ),
    ).exists()
    conditions.append(select(parent.id).join(task, task.task_id == task_id)
        .join(attempt, and_(attempt.attempt_id == attempt_id, attempt.task_id == task.task_id))
        .where(parent.run_identity == run.parent_job_id, parent.job_kind == PARENT_KIND,
            parent.capability_version == "1", parent.parent_job_id.is_(None), parent.branch_depth == 0,
            parent.owner_kind == "user", parent.owner_principal_id == run.owner_principal_id,
            parent.session_id == run.session_id, parent.operator_session_id == run.operator_session_id,
            parent.operator_session_id == parent.session_id, original_session,
            parent.root_run_identity == run.root_run_identity,
            parent.goal_id == run.goal_id, parent.goal_revision == run.goal_revision,
            parent.deadline_at > now,
            task.capability_id == PARENT_CAPABILITY, task.goal_id == run.goal_id, task.goal_revision == run.goal_revision,
            task.owner_principal_id == run.owner_principal_id, task.owner_session_id == run.session_id,
            task.typed_input_digest == func.json_extract(parent.declared_authority_json, "$.typed_input_digest"),
            task.input_artifact_id == func.json_extract(parent.declared_authority_json, "$.input_artifact_id"),
            attempt.workflow_run_id == parent.run_identity,
            attempt.ended_at.is_(None), attempt.cancel_requested_at.is_(None), creation, current_phase).exists())
    return True


async def assert_research_parent_current(db, run):
    if run.job_kind != CHILD_KIND:
        return
    from datetime import datetime, timezone
    from src.workflows.job_runtime import DurableJobLeaseError
    conditions = [WorkflowRunState.run_identity == run.run_identity]
    append_research_parent_gate(conditions, run, now=datetime.now(timezone.utc))
    if await db.scalar(select(WorkflowRunState.id).where(*conditions)) is None:
        raise DurableJobLeaseError("canonical research parent creation/current phase is unavailable")


from contextlib import asynccontextmanager
from contextvars import ContextVar

_programme_policy = ContextVar("native_goal_discovery_policy", default=None)


def current_discovery_witness():
    import asyncio
    held = _programme_policy.get()
    if held is None or held[0] is not asyncio.current_task() or held[2] is None:
        from src.workflows.job_runtime import DurableJobTransitionError
        raise DurableJobTransitionError("programme physical witness unavailable")
    return held[2]


@asynccontextmanager
async def discovery_writer_scope(*, witness=None):
    """Current configuration owner fences only the short native writer.

    Release before any physical HTTP or artifact work. Possession of a staged
    policy never authenticates an issuer or grants a new generation.
    """
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.guardian.goal_programmes import stage_programme_policy
    import asyncio
    held = _programme_policy.get()
    if held is not None and held[0] is asyncio.current_task():
        if witness is not None and held[2] is not witness:
            token = _programme_policy.set((held[0], held[1], witness))
            try:
                yield held[1]
            finally:
                _programme_policy.reset(token)
            return
        yield held[1]
        return
    async with configuration_mutation_lock:
        policy = stage_programme_policy()
        token = _programme_policy.set((asyncio.current_task(), policy, witness))
        try:
            yield policy
        finally:
            _programme_policy.reset(token)


def _discovery_guard_numeric_entries(job_id, widths, row_upper, *, part):
    """Original authority/model and persisted readback construction grammar.

    A raw UTF-8 JSON byte contributes at most six ASCII canonical units after
    parsing/escaping. The fixed models retain no additional source body. Each
    parser, nested model, canonical string, encoding and digest is a separate
    paid appearance; this function builds numbers and labels only.
    """
    entries = []
    def add(name, upper):
        entries.append((("discovery-research-guard", job_id, name), upper))
    if part == "authority":
        upper = 6 * widths["declared_authority_json"] + 2
        for name in ("authority-json", "authority-model", "programme-binding-model",
                "plan-ref-model", "authority-return"):
            add(name, upper)
        add("occurrence-date", 12)
    elif part == "lookup":
        add("lookup-model", row_upper)
        add("lookup-result", row_upper + 2)
    elif part == "readback":
        for field in ("checkpoint_receipts_json", "artifact_receipts_json"):
            upper = 6 * widths[field] + 2
            for operation in ("json", "canonical", "encoded"):
                add((field, operation), upper)
            add((field, "digest"), 66)
    else:
        from src.memory.header_bounds import HeaderBoundsError
        raise HeaderBoundsError("programme_research_guard_numeric_part_invalid")
    return tuple(entries)


def original_discovery_research_guard_numeric_schedule(discovery_session, *, constructor):
    """Closed two-A/one-B owner schedule, or accepted-B only; no body read."""
    from src.memory.header_bounds import HeaderBoundsError
    owned = discovery_session.jobs._validate_discovery_session(discovery_session)
    if (constructor is not owned._constructor_bounds or owned._candidate is None
            or constructor["job_id"] != owned._candidate[0]):
        raise HeaderBoundsError("programme_constructor_numeric_inputs_changed")
    widths, row_upper, job_id = constructor["text_bytes"], constructor["row_header_bytes"], constructor["job_id"]
    entries = []
    for phase, count in (("admission", 0 if constructor.get("persisted_queue_only") is True else 2),
            ("queue", 1)):
        for _ in range(count):
            for part in ("authority", "lookup", "readback"):
                entries.extend((phase, appearance, upper) for appearance, upper in
                    _discovery_guard_numeric_entries(job_id, widths, row_upper, part=part))
            # Run=None selects the genuine current WRS when present. Absence
            # consumes no body fee; the unused future allowance stays spent.
            entries.append((phase, ("body", "workflow_run_states", job_id), row_upper))
    return tuple(entries)


def _discovery_guard_prepay(owned, *, part, value=None, run=None, row_upper=None):
    """Use authenticated owner numbers before original parser/model effects."""
    from src.memory.header_bounds import _DISCOVERY_NUMERIC_READS, HeaderBoundsError
    from src.workflows.job_runtime import _discovery_utf8_width, _discovery_encoded_upper
    owned.jobs._validate_discovery_session(owned)
    if owned._candidate is None:
        raise HeaderBoundsError("programme_research_guard_numeric_owner_changed")
    active = _DISCOVERY_NUMERIC_READS.get()
    if active is not None:
        if active.owner is not owned or active.budget is not owned.header_budget:
            raise HeaderBoundsError("programme_research_guard_numeric_owner_changed")
        active._checked()
        widths = active.constructor["text_bytes"]
        upper = active.constructor["row_header_bytes"]
    else:
        # Current reads use the genuine paid source values, never a projected
        # authority, grant or certificate. Counting text creates no encoding.
        widths = {}
        upper = row_upper or 0
        if value is not None:
            widths["declared_authority_json"] = (_discovery_utf8_width(value)
                if type(value) is str else _discovery_encoded_upper(value))
        if run is not None:
            for name in ("checkpoint_receipts_json", "artifact_receipts_json"):
                widths[name] = _discovery_utf8_width(getattr(run, name))
    if value is not None:
        actual = (_discovery_utf8_width(value) if type(value) is str else _discovery_encoded_upper(value))
        # Dict input is already materialized by its original caller, but the
        # model construction remains this owner's next appearance.
        ceiling = (widths["declared_authority_json"] if type(value) is str
            else 6 * widths["declared_authority_json"] + 2)
        if actual > ceiling:
            raise HeaderBoundsError("programme_research_guard_numeric_body_changed")
    if run is not None:
        if run.run_identity != owned._candidate[0]:
            raise HeaderBoundsError("programme_research_guard_numeric_body_changed")
        for name in ("checkpoint_receipts_json", "artifact_receipts_json"):
            if _discovery_utf8_width(getattr(run, name)) > widths[name]:
                raise HeaderBoundsError("programme_research_guard_numeric_body_changed")
    for appearance, amount in _discovery_guard_numeric_entries(owned._candidate[0], widths, upper, part=part):
        owned.header_budget.debit(amount, appearance=appearance)


async def assert_discovery_authority(db, value, *, run=None, discovery_session=None, native_programme_source=None):
    """DB-only programme validation in the existing original job writer."""
    from src.work_board.research_parent import discovery_authority, DISCOVERY_KIND, DISCOVERY_SERVICE
    from src.guardian.goal_programmes import goal_programme_service
    from src.workflows.job_runtime import DurableJobTransitionError
    native_selection = {}
    if native_programme_source is not None:
        from src.runtime_plugins.programme_producer import _ProgrammeSource
        if type(native_programme_source) is not _ProgrammeSource or discovery_session is not None:
            raise DurableJobTransitionError("native_programme_source_unavailable")
        guard, identity = native_programme_source.authority_selection(db)
        native_selection = {"original_admission_guard": guard, "identity_certificate": identity,
            "native_programme_source": native_programme_source}
    owned = (discovery_session.jobs._validate_discovery_session(discovery_session, db=db)
        if discovery_session is not None else None)
    if owned is not None:
        _discovery_guard_prepay(owned, part="authority", value=value)
    if native_programme_source is not None:
        from src.workflows.job_runtime import _discovery_utf8_width
        widths = {"declared_authority_json": _discovery_utf8_width(value)}
        for appearance, amount in _discovery_guard_numeric_entries(native_programme_source.job_id, widths, 0, part="authority"):
            native_programme_source.header_budget.debit(amount, appearance=appearance)
    authority = discovery_authority(value)
    import asyncio
    held = _programme_policy.get()
    if held is None or held[0] is not asyncio.current_task():
        raise DurableJobTransitionError("programme transition requires its native configuration writer scope")
    policy = held[1]
    binding = authority.programme_binding
    if owned is not None and (owned._candidate is None or authority.original_job_id != owned._candidate[0]
            or binding != owned._candidate[1]):
        raise DurableJobTransitionError("programme original discovery candidate changed")
    if run is None:
        if owned is None:
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == authority.original_job_id))
        else:
            from src.memory.composition_headers import snapshot_reads_async, charge_row
            common = await owned.guard._original_discovery_common(owned)
            async with snapshot_reads_async(db, common):
                row = common.rows.get(("workflow_run_states", authority.original_job_id))
                if row is not None:
                    connection = await db.connection()
                    await connection.run_sync(lambda current: charge_row(
                        current, "workflow_run_states", authority.original_job_id))
                    _discovery_guard_prepay(owned, part="lookup", row_upper=row[1])
                run = await db.scalar(select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == authority.original_job_id))
                if (run is None) != (row is None):
                    raise DurableJobTransitionError("programme original discovery row changed")
    if run is not None and (run.job_kind != DISCOVERY_KIND or run.owner_kind != "service"
            or run.service_id != DISCOVERY_SERVICE or run.owner_principal_id != DISCOVERY_SERVICE
            or run.session_id is not None or run.operator_session_id is not None
            or run.run_identity != authority.original_job_id or run.parent_job_id
            or run.branch_depth != 0 or run.goal_id != binding.goal_id
            or run.goal_revision != binding.goal_revision):
        raise DurableJobTransitionError("programme original native lineage changed")
    if run is not None:
        from src.workflows.research_sources import DiscoveryInputWitness
        from src.workflows.job_runtime import _digest
        witness = held[2]
        if owned is not None:
            _discovery_guard_prepay(owned, part="readback", run=run)
        if native_programme_source is not None:
            widths = {name: _discovery_utf8_width(getattr(run, name))
                for name in ("checkpoint_receipts_json", "artifact_receipts_json")}
            for appearance, amount in _discovery_guard_numeric_entries(native_programme_source.job_id, widths, 0, part="readback"):
                native_programme_source.header_budget.debit(amount, appearance=appearance)
        if (not isinstance(witness, DiscoveryInputWitness) or witness.job_id != run.run_identity
                or witness.input_digest != run.input_digest or witness.authority_digest != run.authority_digest
                or witness.checkpoint_digest != _digest(json.loads(run.checkpoint_receipts_json))
                or witness.artifact_digest != _digest(json.loads(run.artifact_receipts_json))):
            raise DurableJobTransitionError("programme physical readback is not current at this writer")
    programme = await goal_programme_service.validate_current_binding(db=db, binding=binding, policy=policy, **native_selection,
        **({"discovery_session": discovery_session} if discovery_session is not None else {}))
    from src.guardian.research_plan_contracts import STAGES
    if owned is None:
        if not {capability for _, capability, _ in STAGES} <= set(programme.capability_ids):
            raise DurableJobTransitionError("programme fixed stage capability grant changed")
    else:
        # Same fixed-stage subset predicate over the validator's paid original
        # list. Membership does not materialize another copy of its grants.
        for _, capability, _ in STAGES:
            if capability not in programme.capability_ids:
                raise DurableJobTransitionError("programme fixed stage capability grant changed")
    return programme
