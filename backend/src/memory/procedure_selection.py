"""Current exact procedure preferences affect suggestions only."""
from sqlalchemy import select, func, text

from src.db import engine as db_engine
from src.db.models import Memory, MemoryProposal, MemoryProposalStatus, MemoryStatus, MemoryTombstone
from src.memory.procedure_recommendations import PROPOSAL_SCHEMA, bounded_json, digest, assert_current_root, stage_procedure_bundle
from src.memory.procedure_recommendation_job import preference_scope, recheck_bundle, pin_current_package
from src.memory.procedure_preferences import proposal_projection
from src.memory.repository import _effect_mac_key, _m5_selection_binding_matches, _canonical_memory_deletion_marker
from src.work_board.repository import BoardError


async def current_procedure_preference(operator, *, routine_id, version, routine_revision, goal_id, goal_revision):
    """No mutation/invocation. Never treat a stored subset as current truth."""
    async with db_engine.get_session() as db:
        await assert_current_root(db, operator)
        rows = list((await db.execute(select(MemoryProposal).where(
            MemoryProposal.schema_version == PROPOSAL_SCHEMA,
            MemoryProposal.owner_principal_id == operator.principal.principal_id,
            MemoryProposal.owner_session_id == operator.session_id,
            MemoryProposal.goal_id == goal_id, MemoryProposal.goal_revision == goal_revision,
            MemoryProposal.status == MemoryProposalStatus.accepted,
            func.json_extract(MemoryProposal.memory_scope_json, "$.routine_id") == routine_id,
            func.json_extract(MemoryProposal.memory_scope_json, "$.version") == version,
        ).order_by(MemoryProposal.accepted_at.desc(), MemoryProposal.proposal_id).limit(21))).scalars().all())
        if len(rows) > 20:
            return {"status": "blocked", "reason_code": "procedure_preference_history_limit", "memory_status": "no_learning"}
        if not rows:
            return {"status": "none", "reason_code": "no_adopted_procedure_preference", "memory_status": "no_learning"}
        ids = [row.proposal_id for row in rows]
    signing_key = _effect_mac_key()
    try:
        bundle = await stage_procedure_bundle(operator, routine_id=routine_id, version=version,
            routine_revision=routine_revision, goal_id=goal_id, goal_revision=goal_revision)
        with pin_current_package(bundle.scope):
            async with db_engine.get_session() as db:
                await db.execute(text("BEGIN"))
                await recheck_bundle(db, operator, bundle)
                # The selection read uses one consistent canonical snapshot
                # for full membership, proposal, memory and tombstone state.
                current_rows = list((await db.execute(select(MemoryProposal).where(
                    MemoryProposal.schema_version == PROPOSAL_SCHEMA,
                    MemoryProposal.owner_principal_id == operator.principal.principal_id,
                    MemoryProposal.owner_session_id == operator.session_id,
                    MemoryProposal.goal_id == goal_id, MemoryProposal.goal_revision == goal_revision,
                    MemoryProposal.status == MemoryProposalStatus.accepted,
                    func.json_extract(MemoryProposal.memory_scope_json, "$.routine_id") == routine_id,
                    func.json_extract(MemoryProposal.memory_scope_json, "$.version") == version,
                ).order_by(MemoryProposal.accepted_at.desc(), MemoryProposal.proposal_id).limit(21))).scalars().all())
                if [row.proposal_id for row in current_rows] != ids:
                    return {"status": "blocked", "reason_code": "procedure_preference_changed", "memory_status": "no_learning"}
                expected_scope = preference_scope(bundle)
                for row in current_rows:
                    if bounded_json(row.memory_scope_json) != expected_scope:
                        continue
                    memory = await db.get(Memory, row.accepted_memory_id, populate_existing=True) if row.accepted_memory_id else None
                    tombstone = (await db.execute(select(MemoryTombstone).where(
                        MemoryTombstone.memory_id == row.accepted_memory_id))).scalar_one_or_none() if memory else None
                    if (memory is None or memory.status != MemoryStatus.active or tombstone is not None
                        or _canonical_memory_deletion_marker(memory) is not None or memory.source_session_id != operator.session_id
                        or digest_text(memory.content) != row.accepted_memory_content_digest):
                        continue
                    provenance = bounded_json(memory.metadata_json, {}).get("work_board_provenance", {})
                    if not _m5_selection_binding_matches(provenance, proposal_id=row.proposal_id,
                        accepted_content_digest=row.accepted_memory_content_digest, decision_effect=row.decision_effect,
                        memory_scope=expected_scope, source_binding=row, _signing_key=signing_key):
                        continue
                    return {"status": "suggested", "reason_code": "adopted_reviewed_procedure_preference",
                        "memory_status": "accepted", "suggested_version_id": bundle.scope.version_id,
                        "suggested_routine_id": routine_id, "suggested_version": version,
                        "review": proposal_projection(row)}
        return {"status": "blocked", "reason_code": "procedure_preference_stale", "memory_status": "no_learning"}
    except BoardError as exc:
        return {"status": "blocked", "reason_code": exc.code, "memory_status": "no_learning"}


def digest_text(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()
