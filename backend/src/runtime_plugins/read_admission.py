"""Closed server-only candidates for the four adopted native metadata reads.

These values carry no authority. The canonical admission and claim writer must
bind them to the original live Root, composition, attempt and deadline.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .contracts import object_fields, ref, sha, text, enum, nullable_ref
from .protocol import integer, ProtocolError, MAX_FRAME

READ_JOB_KIND = "runtime_service_read_v1"
READ_METHODS = frozenset({"capabilities.list", "capabilities.describe", "connections.inspect", "memory.retrieve"})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def validate_read_candidate(value):
    if type(value) is not dict or value.get("method") not in READ_METHODS:
        raise ProtocolError("unsupported native read candidate")
    common = {"schema_version": lambda v: integer(v, 1, 1), "method": lambda v: enum(v, READ_METHODS)}
    fields = {
        "capabilities.list": {"cursor": nullable_ref, "limit": lambda v: integer(v, 1, 50), "native_inventory_digest": sha},
        "capabilities.describe": {"capability_id": ref, "native_inventory_digest": sha},
        "connections.inspect": {"connection_ref": ref, "expected_connection_revision": lambda v: integer(v, 1)},
        "memory.retrieve": {"query": lambda v: text(v, 200), "limit": lambda v: integer(v, 1, 20), "status": lambda v: enum(v, {"active"})},
    }
    object_fields(value, {**common, **fields[value["method"]]})
    return dict(value)


def native_capability_inventory():
    """Only bundled native metadata; never enumerate MCP or workflow sources."""
    from src.native_tools.registry import TOOL_METADATA
    from src.extensions.capability_contract import build_native_tool_contract
    inventory = []
    for name, metadata in sorted(TOOL_METADATA.items()):
        contract = build_native_tool_contract(
            name=name, description=metadata.get("description", ""),
            policy_modes=list(metadata.get("policy_modes", [])),
            risk_level=metadata.get("risk_level", "low"),
            execution_boundaries=list(metadata.get("execution_boundaries", [])),
            availability="ready", blocked_reason=None, execution=metadata.get("execution"),
        )
        inventory.append({"capability_id": "native_tool:" + name, "version": contract["version"],
            "state": "available", "reason_code": None})
    return inventory, _digest(inventory)


def capability_read_projection(candidate):
    from .dispatch import NativeServiceBlocked
    candidate = validate_read_candidate(candidate)
    inventory, digest = native_capability_inventory()
    if candidate.get("native_inventory_digest") != digest:
        raise NativeServiceBlocked("native_capability_inventory_changed")
    if candidate["method"] == "capabilities.describe":
        matches = [entry for entry in inventory if entry["capability_id"] == candidate["capability_id"]]
        if len(matches) != 1:
            raise NativeServiceBlocked("native_capability_candidate_unavailable")
        return matches[0]
    if candidate["method"] != "capabilities.list":
        raise NativeServiceBlocked("native_capability_candidate_not_bound")
    offset = 0
    if candidate["cursor"] is not None:
        positions = [index for index, entry in enumerate(inventory) if entry["capability_id"] == candidate["cursor"]]
        if len(positions) != 1:
            raise NativeServiceBlocked("native_capability_cursor_unavailable")
        offset = positions[0] + 1
    page = inventory[offset:offset + candidate["limit"]]
    next_cursor = page[-1]["capability_id"] if page and offset + len(page) < len(inventory) else None
    return {"capabilities": page, "next_cursor": next_cursor}


@dataclass(frozen=True)
class NativeServiceReadAdmission:
    """Immutable exact candidate, constructed only by authenticated owners."""
    candidate_json: str
    candidate_digest: str

    @classmethod
    def from_candidate(cls, candidate):
        candidate = validate_read_candidate(candidate)
        return cls(json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False), _digest(candidate))

    def candidate(self):
        value = validate_read_candidate(json.loads(self.candidate_json))
        if _digest(value) != self.candidate_digest:
            raise ProtocolError("native read candidate changed")
        return value

    def wire_inputs(self, job_id):
        ref(job_id)
        value = self.candidate()
        method = value["method"]
        if method == "memory.retrieve":
            return {"query_ref": job_id, "limit": value["limit"]}
        if method == "capabilities.list":
            return {"cursor": value["cursor"], "limit": value["limit"]}
        if method == "capabilities.describe":
            return {"capability_id": value["capability_id"]}
        return {"connection_ref": value["connection_ref"]}


async def memory_read_projection(db, *, owner_session_id, candidate, repository=None):
    """Reuse only the original owner list-safe projection in the fenced session."""
    from src.memory.repository import memory_repository
    from .dispatch import NativeServiceBlocked
    candidate = validate_read_candidate(candidate)
    if candidate["method"] != "memory.retrieve":
        raise NativeServiceBlocked("native_memory_candidate_not_bound")
    page = await (repository or memory_repository)._list_memory_records_in_session(
        db, owner_session_id=owner_session_id, query=candidate["query"],
        limit=candidate["limit"], status="active", recovered_read_scopes=None,
    )
    records = []
    for record in page["records"]:
        summary = record["summary"]
        if summary == "[redaction unavailable]" or record.get("redaction_state") == "degraded":
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
    if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_FRAME:
        raise NativeServiceBlocked("native_memory_frame_unsupported")
    return value
