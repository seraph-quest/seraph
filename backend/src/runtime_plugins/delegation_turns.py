"""Delegated specialist turns on the Cordis loop (#1020 PR2).

With delegation on, the chat agent is the orchestrator: its only tools are the
specialists. Each managed-agent call becomes one durable child turn of the
chat turn (``cordis_child_turn_v1``, same owner, Root and authority, the
parent's deadline less its settlement window), admitted by the reviewed
``delegation.startChild`` sequencer and then run step by step on the
agent-loop sequencer, exactly as legacy runs the specialist in process. The
child's surface is its reviewed preset intersected with the parent's eligible
surface; children never delegate again and at most ``MAX_CHILDREN`` run per
turn. Cancellation, expiry, a revoked Root, an epoch change or a missing
preset fences the child's effects and its adoption; the child's receipts are
on its own job row and the parent's receipt for the delegating call is
derived from them. ``agent_turns.CordisTurn.delegate`` owns the flow.
"""
from __future__ import annotations

from .agent_turns import MAX_CHILDREN
from .dispatch import NativeServiceBlocked

# Registered in the cutover manifest after the presets every child turn binds.
DELEGATION_DOMAIN = "seraph.delegation.v1"


def validate_delegation_candidate(candidate) -> dict:
    """One child admission: the parent turn, the child's index under the cap and its reviewed child preset."""
    from src.agent.presets import PresetError, preset, preset_digest
    if (type(candidate) is not dict or set(candidate) != {"schema_version", "method", "turn_ref", "projection"}
            or candidate["schema_version"] != 1 or candidate["method"] != "delegation.startChild"
            or type(candidate["turn_ref"]) is not str or not candidate["turn_ref"].startswith("cordis-turn:")):
        raise NativeServiceBlocked("native_delegation_candidate_invalid")
    projection = candidate["projection"]
    if (type(projection) is not dict
            or set(projection) != {"schema_version", "kind", "child_index", "max_children", "preset_id",
                                   "preset_digest"}
            or projection["schema_version"] != 1 or projection["kind"] != "child_admit"
            or projection["max_children"] != MAX_CHILDREN or type(projection["child_index"]) is not int
            or not 0 <= projection["child_index"] < MAX_CHILDREN or type(projection["preset_id"]) is not str):
        raise NativeServiceBlocked("native_delegation_candidate_invalid")
    try:
        if (preset(projection["preset_id"])["kind"] != "child"
                or projection["preset_digest"] != preset_digest(projection["preset_id"])):
            raise NativeServiceBlocked("native_delegation_candidate_invalid")
    except PresetError as exc:
        raise NativeServiceBlocked("native_delegation_candidate_invalid") from exc
    return dict(candidate)
