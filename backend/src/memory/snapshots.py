from __future__ import annotations

import hashlib
import json

from sqlalchemy.exc import SQLAlchemyError

from src.db.models import MemoryKind, MemorySnapshot, MemorySnapshotKind
from src.memory.repository import memory_repository
from src.memory.soul import read_soul

_SESSION_BOUNDED_SNAPSHOT_CACHE: dict[str, tuple[str, str]] = {}


def _extract_soul_section_lines(soul_context: str, section: str, *, limit: int) -> tuple[str, ...]:
    header = f"## {section}".lower()
    lines = soul_context.splitlines()
    in_section = False
    extracted: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if line.lower().startswith("## "):
            if line.lower() == header:
                in_section = True
                continue
            if in_section:
                break
        if not in_section or not line or line.startswith("("):
            continue
        if line.startswith("- "):
            extracted.append(line[2:].strip())
        else:
            extracted.append(line)
        if len(extracted) >= limit:
            break
    return tuple(extracted)


def _dedupe_preserve(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        normalized = " ".join(item.strip().split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)


def _memory_snapshot_text(memory) -> str:
    if memory.kind == MemoryKind.procedural:
        return (memory.content or memory.summary or "").strip()
    return (memory.summary or memory.content or "").strip()


async def _reconcile_snapshot_memory() -> tuple[bool, dict[str, object]]:
    try:
        receipt = await memory_repository.reconcile_memory_tombstones()
    except SQLAlchemyError:
        return False, {"status": "degraded_no_learning"}
    return receipt.get("status") == "ready", receipt


async def _snapshot_tombstone_revision() -> str | None:
    try:
        return await memory_repository.get_memory_tombstone_revision()
    except SQLAlchemyError:
        return None


def _empty_snapshot() -> MemorySnapshot:
    return MemorySnapshot(
        kind=MemorySnapshotKind.bounded_guardian_context,
        content="",
        source_hash=None,
    )


async def render_bounded_guardian_snapshot(
    *,
    soul_context: str | None = None,
) -> tuple[str, str]:
    memory_read_ready, _receipt = await _reconcile_snapshot_memory()
    if not memory_read_ready:
        return "", hashlib.sha256(b"guardian_snapshot_memory_unavailable").hexdigest()

    revision_before = await _snapshot_tombstone_revision()
    if revision_before is None:
        return "", hashlib.sha256(b"guardian_snapshot_memory_unavailable").hexdigest()

    resolved_soul = soul_context if isinstance(soul_context, str) else read_soul()
    try:
        grouped = await memory_repository.list_memories_by_kinds(
            kinds=(
                MemoryKind.goal,
                MemoryKind.commitment,
                MemoryKind.preference,
                MemoryKind.communication_preference,
                MemoryKind.project,
                MemoryKind.collaborator,
                MemoryKind.obligation,
                MemoryKind.routine,
            ),
            limit_per_kind=2,
        )
        procedural_memories = await memory_repository.list_memories(
            kind=MemoryKind.procedural,
            limit=4,
        )
    except SQLAlchemyError:
        return "", hashlib.sha256(b"guardian_snapshot_memory_unavailable").hexdigest()

    revision_after = await _snapshot_tombstone_revision()
    if revision_after is None or revision_after != revision_before:
        return "", hashlib.sha256(b"guardian_snapshot_memory_changed").hexdigest()

    identity_bits = _extract_soul_section_lines(resolved_soul, "Identity", limit=3)
    goal_bits = _dedupe_preserve(
        list(_extract_soul_section_lines(resolved_soul, "Goals", limit=3))
        + [
            _memory_snapshot_text(memory)
            for kind in ("goal", "commitment")
            for memory in grouped.get(kind, [])
        ]
    )
    preference_bits = _dedupe_preserve(
        list(_extract_soul_section_lines(resolved_soul, "Personality Notes", limit=2))
        + [
            _memory_snapshot_text(memory)
            for kind in ("preference", "communication_preference")
            for memory in grouped.get(kind, [])
        ]
    )
    procedural_bits = _dedupe_preserve(
        [_memory_snapshot_text(memory) for memory in procedural_memories]
    )
    project_bits = _dedupe_preserve(
        [_memory_snapshot_text(memory) for memory in grouped.get("project", [])]
    )
    collaborator_bits = _dedupe_preserve(
        [_memory_snapshot_text(memory) for memory in grouped.get("collaborator", [])]
    )
    cadence_bits = _dedupe_preserve(
        [
            _memory_snapshot_text(memory)
            for kind in ("obligation", "routine")
            for memory in grouped.get(kind, [])
        ]
    )

    lines: list[str] = []
    if identity_bits:
        lines.append(f"- Identity: {' | '.join(identity_bits)}")
    if goal_bits:
        lines.append(f"- Goal memory: {' | '.join(goal_bits)}")
    if preference_bits:
        lines.append(f"- Preferences: {' | '.join(preference_bits)}")
    if procedural_bits:
        lines.append(f"- Delivery guidance: {' | '.join(procedural_bits)}")
    if project_bits:
        lines.append(f"- Active projects: {' | '.join(project_bits)}")
    if collaborator_bits:
        lines.append(f"- Collaborators: {' | '.join(collaborator_bits)}")
    if cadence_bits:
        lines.append(f"- Routines and obligations: {' | '.join(cadence_bits)}")

    payload = {
        "identity": list(identity_bits),
        "goal_memory": list(goal_bits),
        "preferences": list(preference_bits),
        "delivery_guidance": list(procedural_bits),
        "active_projects": list(project_bits),
        "collaborators": list(collaborator_bits),
        "routines_and_obligations": list(cadence_bits),
    }
    content = "\n".join(lines[:7])
    source_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return content, source_hash


async def refresh_bounded_guardian_snapshot(
    *,
    soul_context: str | None = None,
):
    try:
        content, source_hash = await render_bounded_guardian_snapshot(soul_context=soul_context)
        revision = await _snapshot_tombstone_revision()
        if revision is None:
            return _empty_snapshot()
        current = await memory_repository.get_snapshot(MemorySnapshotKind.bounded_guardian_context)
        if current is not None and current.source_hash == source_hash and current.content == content:
            return current
        return await memory_repository.save_snapshot(
            kind=MemorySnapshotKind.bounded_guardian_context,
            content=content,
            source_hash=source_hash,
            canonical_tombstone_revision=revision,
        )
    except (SQLAlchemyError, RuntimeError):
        return _empty_snapshot()


async def get_or_create_bounded_guardian_snapshot(
    *,
    soul_context: str | None = None,
    session_id: str | None = None,
) -> str:
    memory_read_ready, receipt = await _reconcile_snapshot_memory()
    if not memory_read_ready:
        if session_id is not None:
            _SESSION_BOUNDED_SNAPSHOT_CACHE.pop(session_id, None)
        return ""
    if int(receipt.get("reapplied_count") or 0) > 0:
        _SESSION_BOUNDED_SNAPSHOT_CACHE.clear()

    if session_id is not None:
        cached = _SESSION_BOUNDED_SNAPSHOT_CACHE.get(session_id)
        if cached is not None:
            cached_content, cached_revision = cached
            current_revision = await _snapshot_tombstone_revision()
            if current_revision is None:
                _SESSION_BOUNDED_SNAPSHOT_CACHE.pop(session_id, None)
                return ""
            if cached_revision == current_revision and cached_content.strip():
                return cached_content
            _SESSION_BOUNDED_SNAPSHOT_CACHE.pop(session_id, None)

    try:
        content, source_hash = await render_bounded_guardian_snapshot(soul_context=soul_context)
        revision = await _snapshot_tombstone_revision()
        if revision is None:
            return ""
        current = await memory_repository.get_snapshot(MemorySnapshotKind.bounded_guardian_context)
        if current is None or current.source_hash != source_hash or current.content != content:
            current = await memory_repository.save_snapshot(
                kind=MemorySnapshotKind.bounded_guardian_context,
                content=content,
                source_hash=source_hash,
                canonical_tombstone_revision=revision,
            )
    except (SQLAlchemyError, RuntimeError):
        if session_id is not None:
            _SESSION_BOUNDED_SNAPSHOT_CACHE.pop(session_id, None)
        return ""
    if session_id is not None and current.content.strip():
        _SESSION_BOUNDED_SNAPSHOT_CACHE[session_id] = (
            current.content,
            str(current.canonical_tombstone_revision or revision),
        )
        return current.content
    if current is not None and current.content.strip():
        return current.content
    snapshot = await refresh_bounded_guardian_snapshot(soul_context=soul_context)
    if session_id is not None and snapshot.content.strip():
        snapshot_revision = snapshot.canonical_tombstone_revision
        if snapshot_revision is not None:
            _SESSION_BOUNDED_SNAPSHOT_CACHE[session_id] = (
                snapshot.content,
                snapshot_revision,
            )
    return snapshot.content


def _reset_bounded_guardian_snapshot_cache() -> None:
    _SESSION_BOUNDED_SNAPSHOT_CACHE.clear()


def invalidate_bounded_guardian_snapshot_cache(*, session_id: str | None = None) -> None:
    if session_id is None:
        _SESSION_BOUNDED_SNAPSHOT_CACHE.clear()
        return
    _SESSION_BOUNDED_SNAPSHOT_CACHE.pop(session_id, None)
