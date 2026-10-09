"""Goal privacy preflight; never cancels or settles an original job.

This conservative owner has no positive physical-quiescence issuer. A live
unsupported dispatcher therefore blocks this operation before its effects.
Historical metadata is read only after a bounded scalar header; absence of a
recovery address is not a receipt for a running process.
"""
from datetime import datetime, timezone

from sqlalchemy import text
from sqlmodel import select

from src.db.models import WorkflowRunState
from src.goals.repository import GoalOwnershipConflict
from src.memory.header_bounds import (
    HeaderBoundsError, MAX_BYTES, WRS_LEGACY_PARENT,
    preflight_exact_rows, validate_certificate, strict_json_loads,
)


class GoalSourceClosureBlocked(GoalOwnershipConflict):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fixed_text(column, value):
    # All values here are source constants. SQLite CASE is lazy; the body is
    # never compared until its storage type and exact octet size are known.
    return (f"(CASE WHEN typeof({column})='text' THEN CASE WHEN "
            f"octet_length({column})={len(value.encode('utf-8'))} THEN CASE WHEN "
            f"{column} COLLATE BINARY='{value}' THEN 1 ELSE 0 END "
            "ELSE 0 END ELSE 0 END=1)")


_TERMINAL = " OR ".join(_fixed_text("w.status", item)
                        for item in ("succeeded", "degraded", "cancelled"))
_POSSIBLY_LIVE = (f"({_fixed_text('w.status', 'running')} OR w.lease_owner IS NOT NULL "
                  "OR w.lease_expires_at IS NOT NULL OR "
                  f"(NOT ({_TERMINAL}) AND w.approval_context_json IS NOT NULL "
                  "AND w.finished_at IS NULL))")
_UNCERTIFIED_METADATA = (
    "(CASE WHEN typeof(w.record_schema_version)='integer' THEN CASE WHEN "
    "w.record_schema_version<2 THEN CASE WHEN w.metadata_json IS NOT NULL "
    "THEN 1 ELSE 0 END ELSE 0 END ELSE CASE WHEN w.metadata_json IS NOT NULL "
    "THEN 1 ELSE 0 END END=1) AND NOT " + _fixed_text("w.metadata_json", "{}")
)


def _lease_absent(metadata):
    if type(metadata) is not dict:
        return False
    if "orchestration_v2" not in metadata:
        return True
    orchestration = metadata["orchestration_v2"]
    if type(orchestration) is not dict:
        return False
    return "lease" not in orchestration or (
        type(orchestration["lease"]) is dict and not orchestration["lease"])


async def assert_goal_job_privacy_current(db, goal_ids):
    """Same original Goal writer; complete scalar EXISTS, bounded diagnostics.

No caller-supplied checked IDs, owner labels, expiry, or finished status can
prove an old dispatcher stopped. Memory portability overflow alone is not a
privacy denial: only a concrete unsupported live row/recovery address is.
"""
    # One fixed actual fresh-child source is implemented by the original
    # compatibility owner. This does not prove its historical parent stopped.
    from src.workflows.durable_state import (
        _LegacyHeaderBudget, _append_legacy_parent_condition, _verify_legacy_child_in_session,
    )
    checked_rowids = []
    candidates = []
    budget = _LegacyHeaderBudget()  # byte evidence only; never Source authority
    for goal_id in goal_ids:
        if len(candidates) >= 129:
            break
        candidates.extend((await db.execute(text(
            "SELECT w._rowid_, CASE WHEN typeof(w.id)='text' THEN CASE WHEN "
            "octet_length(w.id) BETWEEN 1 AND 512 THEN w.id END END "
            "FROM workflow_run_states AS w INDEXED BY ix_workflow_run_states_goal_id "
            f"WHERE w.goal_id=:goal AND {_POSSIBLY_LIVE} ORDER BY w._rowid_ LIMIT :limit"),
            {"goal": goal_id, "limit": 129-len(candidates)})).all())
    certificate = None
    if candidates and len(candidates) <= 128 and all(type(row[1]) is str for row in candidates):
        try:
            certificate = await budget.certify(db, WRS_LEGACY_PARENT, tuple(row[1] for row in candidates))
        except HeaderBoundsError:
            pass
    if certificate is not None:
        for rowid, row_id in candidates:
            await validate_certificate(db, certificate)
            child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.id == row_id)
                                    .execution_options(populate_existing=True))
            try:
                snapshot = None if child is None else await _verify_legacy_child_in_session(db, child, budget=budget)
            except (HeaderBoundsError, RuntimeError):
                # An unavailable actual source or exhausted shared byte budget
                # never enters the checked set; the complete live EXISTS wins.
                continue
            if snapshot is not None:
                conditions = [WorkflowRunState.id == child.id]
                if _append_legacy_parent_condition(conditions, child, writer_db=db,
                                                   now=datetime.now(timezone.utc)):
                    actual_id = await db.scalar(select(WorkflowRunState.id).where(*conditions))
                    if actual_id == child.id:
                        checked_rowids.append(rowid)
    checked = {f"checked_{i}": rowid for i, rowid in enumerate(checked_rowids)}
    live_exclusions = "".join(f" AND w._rowid_<>:checked_{i}" for i in range(len(checked_rowids)))
    for goal_id in goal_ids:
        live = await db.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM workflow_run_states AS w "
            "INDEXED BY ix_workflow_run_states_goal_id WHERE w.goal_id=:goal "
            f"AND {_POSSIBLY_LIVE}{live_exclusions})"), {**checked, "goal": goal_id})
        if live:
            raise GoalSourceClosureBlocked("goal_live_source_unproven")

    absent_rowids = []
    selected = 0
    for goal_id in goal_ids:
        if selected >= 129:
            break
        headers = (await db.execute(text(
            "SELECT w._rowid_, typeof(w.metadata_json), octet_length(w.metadata_json) "
            "FROM workflow_run_states AS w INDEXED BY ix_workflow_run_states_goal_id "
            f"WHERE w.goal_id=:goal AND {_UNCERTIFIED_METADATA} "
            "ORDER BY w._rowid_ LIMIT :limit"),
            {"goal": goal_id, "limit": 129-selected})).all()
        selected += len(headers)
        for rowid, storage_type, octets in headers:
            # Leave any uncertified row out of the absence set. The complete
            # EXISTS below, rather than a limit/portability error, decides.
            upper = 6*octets+130 if type(octets) is int and octets >= 0 else MAX_BYTES+1
            if storage_type != "text" or upper > budget.remaining or len(absent_rowids) >= 128:
                continue
            encoding = await db.scalar(text("PRAGMA encoding"))
            version = await db.scalar(text("SELECT sqlite_version()"))
            if encoding != "UTF-8" or tuple(map(int, version.split('.'))) < (3, 43, 0):
                continue
            resource_ref = ("workflow_run_states", "_rowid_", str(rowid))
            if len(budget.references | {resource_ref}) > 128:
                continue
            budget.references.add(resource_ref)
            budget.remaining -= upper
            raw = await db.scalar(text(
                "SELECT metadata_json FROM workflow_run_states WHERE _rowid_=:rowid"),
                {"rowid": rowid})
            try:
                metadata = strict_json_loads(raw, max_utf8_bytes=octets)
            except HeaderBoundsError:
                continue
            if _lease_absent(metadata):
                absent_rowids.append(rowid)

    binds = {f"absent_{i}": rowid for i, rowid in enumerate(absent_rowids)}
    exclusions = "".join(f" AND w._rowid_<>:absent_{i}" for i in range(len(absent_rowids)))
    for goal_id in goal_ids:
        unresolved = await db.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM workflow_run_states AS w "
            "INDEXED BY ix_workflow_run_states_goal_id WHERE w.goal_id=:goal "
            f"AND {_UNCERTIFIED_METADATA}{exclusions})"), {**binds, "goal": goal_id})
        if unresolved:
            raise GoalSourceClosureBlocked("goal_legacy_dispatch_quiescence_unproven")
