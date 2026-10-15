"""Closed server-only candidates for the four native metadata reads.

Candidates carry no authority: the canonical admission and the live original
invocation bind them to the current Root, composition, attempt and deadline.
Projections reuse only the existing owners' list-safe/metadata-only views.
"""
from __future__ import annotations

import hashlib
import json

from .contracts import enum, nullable_ref, object_fields, ref, sha, text
from .protocol import MAX_FRAME, ProtocolError, integer

READ_METHODS = frozenset({"capabilities.list", "capabilities.describe", "connections.inspect", "memory.retrieve"})
REDACTION_UNAVAILABLE = "[redaction unavailable]"


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def validate_read_candidate(value) -> dict:
    if type(value) is not dict or value.get("method") not in READ_METHODS:
        raise ProtocolError("unsupported native read candidate")
    common = {"schema_version": lambda v: integer(v, 1, 1), "method": lambda v: enum(v, READ_METHODS)}
    fields = {
        "capabilities.list": {"cursor": nullable_ref, "limit": lambda v: integer(v, 1, 50), "native_inventory_digest": sha},
        "capabilities.describe": {"capability_id": ref, "native_inventory_digest": sha},
        "connections.inspect": {"connection_ref": ref, "expected_connection_revision": lambda v: integer(v, 1)},
        "memory.retrieve": {"query": lambda v: text(v, 200), "limit": lambda v: integer(v, 1, 20),
                            "status": lambda v: enum(v, {"active"})},
    }
    object_fields(value, {**common, **fields[value["method"]]})
    return dict(value)


def wire_inputs(candidate: dict, job_id: str) -> dict:
    """The exact plugin-visible request; private query text never crosses."""
    ref(job_id)
    candidate = validate_read_candidate(candidate)
    method = candidate["method"]
    if method == "memory.retrieve":
        return {"query_ref": job_id, "limit": candidate["limit"]}
    if method == "capabilities.list":
        return {"cursor": candidate["cursor"], "limit": candidate["limit"]}
    if method == "capabilities.describe":
        return {"capability_id": candidate["capability_id"]}
    return {"connection_ref": candidate["connection_ref"]}


def native_capability_inventory() -> tuple[list[dict], str]:
    """Only bundled native tool metadata; never enumerate MCP or workflow sources."""
    from src.extensions.capability_contract import build_native_tool_contract
    from src.native_tools.registry import TOOL_METADATA
    inventory = []
    for name, metadata in sorted(TOOL_METADATA.items()):
        contract = build_native_tool_contract(
            name=name, description=metadata.get("description", ""),
            policy_modes=list(metadata.get("policy_modes", [])),
            risk_level=metadata.get("risk_level", "low"),
            execution_boundaries=list(metadata.get("execution_boundaries", [])),
            availability="ready", blocked_reason=None, execution=metadata.get("execution"))
        inventory.append({"capability_id": "native_tool:" + name, "version": contract["version"],
                          "state": "available", "reason_code": None})
    return inventory, _digest(inventory)


def _capability_projection(candidate: dict) -> dict:
    from .dispatch import NativeServiceBlocked
    inventory, digest = native_capability_inventory()
    if candidate["native_inventory_digest"] != digest:
        raise NativeServiceBlocked("native_capability_inventory_changed")
    if candidate["method"] == "capabilities.describe":
        matches = [entry for entry in inventory if entry["capability_id"] == candidate["capability_id"]]
        if len(matches) != 1:
            raise NativeServiceBlocked("native_capability_candidate_unavailable")
        return matches[0]
    offset = 0
    if candidate["cursor"] is not None:
        positions = [index for index, entry in enumerate(inventory) if entry["capability_id"] == candidate["cursor"]]
        if len(positions) != 1:
            raise NativeServiceBlocked("native_capability_cursor_unavailable")
        offset = positions[0] + 1
    page = inventory[offset:offset + candidate["limit"]]
    next_cursor = page[-1]["capability_id"] if page and offset + len(page) < len(inventory) else None
    return {"capabilities": page, "next_cursor": next_cursor}


async def _connection_projection(db, run, candidate: dict) -> dict:
    """Metadata only; credential references are never selected."""
    from sqlalchemy import select
    from src.db.models import GoogleServiceConnection
    from .dispatch import NativeServiceBlocked
    row = (await db.execute(select(
        GoogleServiceConnection.connection_id, GoogleServiceConnection.revision, GoogleServiceConnection.state,
    ).where(
        GoogleServiceConnection.connection_id == candidate["connection_ref"],
        GoogleServiceConnection.owner_principal_id == run.owner_principal_id,
        GoogleServiceConnection.owner_session_id == run.operator_session_id,
    ))).one_or_none()
    if row is None or row.revision != candidate["expected_connection_revision"]:
        raise NativeServiceBlocked("native_connection_candidate_changed")
    state = row.state if row.state in {"ready", "revoked"} else "blocked"
    return {"connection_ref": row.connection_id, "revision": row.revision, "state": state,
            "reason_code": None if state in {"ready", "revoked"} else "native_connection_unavailable"}


async def _memory_projection(run, candidate: dict, *, repository=None) -> dict:
    """Reuse only the original owner list-safe summary projection."""
    from src.memory.repository import memory_repository
    from .dispatch import NativeServiceBlocked
    page = await (repository or memory_repository).list_memory_records(
        owner_session_id=run.operator_session_id, query=candidate["query"],
        limit=candidate["limit"], status="active", recovered_read_scopes=None)
    records = []
    for record in page["records"]:
        summary = record["summary"]
        if summary == REDACTION_UNAVAILABLE:
            raise NativeServiceBlocked("native_memory_redaction_unavailable")
        if record["summary_truncated"]:
            raise NativeServiceBlocked("native_memory_preview_unsupported")
        emitted = "" if summary is None else summary
        try:
            ref(record["id"])
            text(emitted, 8192)
        except ProtocolError as exc:
            raise NativeServiceBlocked("native_memory_projection_unsupported") from exc
        records.append({"record_ref": record["id"], "text": emitted,
                        "text_digest": hashlib.sha256(emitted.encode("utf-8")).hexdigest()})
    value = {"records": records}
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_FRAME // 2:
        raise NativeServiceBlocked("native_memory_frame_unsupported")
    return value


async def native_read_projection(db, run, candidate: dict) -> dict:
    candidate = validate_read_candidate(candidate)
    if candidate["method"] in {"capabilities.list", "capabilities.describe"}:
        return _capability_projection(candidate)
    if candidate["method"] == "connections.inspect":
        return await _connection_projection(db, run, candidate)
    return await _memory_projection(run, candidate)
