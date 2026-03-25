from __future__ import annotations

import hashlib
import json

from src.db.models import MemoryKind, MemorySnapshotKind
from src.memory.repository import memory_repository
from src.memory.soul import read_soul

_SESSION_BOUNDED_SNAPSHOT_CACHE: dict[str, str] = {}


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


async def render_bounded_guardian_snapshot(
    *,
    soul_context: str | None = None,
) -> tuple[str, str]:
    resolved_soul = soul_context if isinstance(soul_context, str) else read_soul()
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

    identity_bits = _extract_soul_section_lines(resolved_soul, "Identity", limit=3)
    goal_bits = _dedupe_preserve(
        list(_extract_soul_section_lines(resolved_soul, "Goals", limit=3))
        + [
            (memory.summary or memory.content or "").strip()
            for kind in ("goal", "commitment")
            for memory in grouped.get(kind, [])
        ]
    )
    preference_bits = _dedupe_preserve(
        list(_extract_soul_section_lines(resolved_soul, "Personality Notes", limit=2))
        + [
            (memory.summary or memory.content or "").strip()
            for kind in ("preference", "communication_preference")
            for memory in grouped.get(kind, [])
        ]
    )
    project_bits = _dedupe_preserve(
        [(memory.summary or memory.content or "").strip() for memory in grouped.get("project", [])]
    )
    collaborator_bits = _dedupe_preserve(
        [(memory.summary or memory.content or "").strip() for memory in grouped.get("collaborator", [])]
    )
    cadence_bits = _dedupe_preserve(
        [
            (memory.summary or memory.content or "").strip()
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
        "active_projects": list(project_bits),
        "collaborators": list(collaborator_bits),
        "routines_and_obligations": list(cadence_bits),
    }
    content = "\n".join(lines[:6])
    source_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return content, source_hash


async def refresh_bounded_guardian_snapshot(
    *,
    soul_context: str | None = None,
):
    content, source_hash = await render_bounded_guardian_snapshot(soul_context=soul_context)
    current = await memory_repository.get_snapshot(MemorySnapshotKind.bounded_guardian_context)
    if current is not None and current.source_hash == source_hash and current.content == content:
        return current
    return await memory_repository.save_snapshot(
        kind=MemorySnapshotKind.bounded_guardian_context,
        content=content,
        source_hash=source_hash,
    )


async def get_or_create_bounded_guardian_snapshot(
    *,
    soul_context: str | None = None,
    session_id: str | None = None,
) -> str:
    if session_id is not None:
        cached = _SESSION_BOUNDED_SNAPSHOT_CACHE.get(session_id)
        if isinstance(cached, str) and cached.strip():
            return cached

    content, source_hash = await render_bounded_guardian_snapshot(soul_context=soul_context)
    current = await memory_repository.get_snapshot(MemorySnapshotKind.bounded_guardian_context)
    if current is None or current.source_hash != source_hash or current.content != content:
        current = await memory_repository.save_snapshot(
            kind=MemorySnapshotKind.bounded_guardian_context,
            content=content,
            source_hash=source_hash,
        )
    if session_id is not None and current.content.strip():
        _SESSION_BOUNDED_SNAPSHOT_CACHE[session_id] = current.content
        return current.content
    if current is not None and current.content.strip():
        return current.content
    snapshot = await refresh_bounded_guardian_snapshot(soul_context=soul_context)
    if session_id is not None and snapshot.content.strip():
        _SESSION_BOUNDED_SNAPSHOT_CACHE[session_id] = snapshot.content
    return snapshot.content


def _reset_bounded_guardian_snapshot_cache() -> None:
    _SESSION_BOUNDED_SNAPSHOT_CACHE.clear()
