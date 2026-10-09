"""Private indexed reservation membership; original evidence owns authority.

This unhashed projection is maintained by canonical writers, not an integrity
seal against direct database modification. No inference or lifecycle is owned here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re

from sqlmodel import select

KIND = "general_task_group_reservation.v1"
INDEX_NAME = "ix_inference_cost_reservations_owner_group_lookup"


def strict_evidence_entries(evidence_json: str) -> list[dict]:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate evidence key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("non-JSON numeric constant")

    entries = json.loads(evidence_json, object_pairs_hook=unique_object,
        parse_constant=invalid_constant)
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("evidence must be an array of objects")
    markers = [entry for entry in entries if isinstance(entry.get("kind"), str)
        and entry["kind"].startswith("general_task_group_reservation.")]
    if len(markers) > 1 or any(entry["kind"] != KIND for entry in markers):
        raise ValueError("unsupported or duplicate group membership")
    return entries


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def validated_group_entry(row):
    from src.workflows.general_task_accounting import entry_for
    strict_evidence_entries(row.evidence_json)
    entry = entry_for(row)
    if entry is not None:
        group = entry["group"]
        if (not re.fullmatch(r"[0-9a-f]{64}", group["group_id"])
            or _utc(row.deadline_at) > _utc(group["original_deadline_at"])):
            raise ValueError("reservation exceeds original group deadline")
    return entry


def classify_group_lookup(row) -> str:
    from src.workflows.inference_accounting import InferenceAccountingError
    try:
        entry = validated_group_entry(row)
        return "none" if entry is None else "g:" + entry["group"]["group_id"]
    except (ValueError, TypeError, KeyError, AttributeError, InferenceAccountingError):
        return "invalid"


def assert_group_lookup(row) -> None:
    from src.workflows.inference_accounting import InferenceAccountingError
    expected = classify_group_lookup(row)
    if expected == "invalid" or row.group_lookup_key != expected:
        raise InferenceAccountingError("general_task_group_lookup_invalid")


async def group_reservation_rows(db, *, owner_id, group_id, group_digest,
                                 original_root_id, original_deadline_at, group=None):
    """Three exact indexed reads, with twelve rows plus one overflow detector."""
    from src.db.models import InferenceCostReservation as Row
    from src.workflows.inference_accounting import InferenceAccountingError

    def deny():
        raise InferenceAccountingError("general_task_group_lookup_invalid")

    if not re.fullmatch(r"[0-9a-f]{64}", group_id):
        deny()
    for blocked in (None, "invalid"):
        if (await db.execute(select(Row.operation_id).where(
            Row.owner_id == owner_id, Row.group_lookup_key == blocked).limit(1))).first() is not None:
            deny()
    rows = list((await db.execute(select(Row).where(Row.owner_id == owner_id,
        Row.group_lookup_key == "g:" + group_id).limit(13))).scalars())
    if len(rows) > 12:
        deny()
    ordinals = []
    for row in rows:
        assert_group_lookup(row)
        entry = validated_group_entry(row)
        original = entry["group"] if entry is not None else None
        if (original is None or original["group_id"] != group_id
            or entry["group_digest"] != group_digest
            or original["owner_principal_id"] != owner_id
            or original["owner_session_id"] != original_root_id
            or _utc(original["original_deadline_at"]) != _utc(original_deadline_at)
            or (group is not None and original != group.model_dump(mode="json"))):
            deny()
        ordinals.append(entry["call_ordinal"])
    if sorted(ordinals) != list(range(1, len(rows) + 1)):
        deny()
    return rows
