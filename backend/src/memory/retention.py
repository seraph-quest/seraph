"""Finite native Memory row encoding; byte proof confers no owner authority."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

from sqlalchemy import text

from src.memory.header_bounds import (
    HeaderBoundsError, MAX_BYTES, MAX_ROWS, MEMORY_DESCRIPTORS,
    preflight_exact_rows, validate_certificate,
)

MEMORY_VERSION = "native-composition-memory.v1"
MEMORY_RETAINED_FIELDS = {name: descriptor.columns for name, descriptor in MEMORY_DESCRIPTORS.items()}
MEMORY_KEYS = {name: descriptor.key for name, descriptor in MEMORY_DESCRIPTORS.items()}
MEMORY_FLOAT_FIELDS = frozenset({
    ("memories", "confidence"), ("memories", "importance"),
    ("memories", "reinforcement"), ("memory_edges", "weight"),
    ("memory_proposals", "confidence"), ("work_board_proposals", "estimated_cost"),
})


def encode_memory_row(table, key, row):
    """Exact ordered SQLite scalars, preserving private JSON/date/MAC bytes."""
    fields = MEMORY_RETAINED_FIELDS.get(table)
    if (fields is None or type(key) is not str or not key
            or type(row) is not dict or set(row) != set(fields)
            or row[MEMORY_KEYS[table]] != key):
        raise HeaderBoundsError("memory_row_identity_invalid")
    values = []
    for name in fields:
        value = row[name]
        if value is not None and (table, name) in MEMORY_FLOAT_FIELDS:
            if type(value) is not float or not math.isfinite(value):
                raise HeaderBoundsError("memory_float_invalid")
            value = {"type": "float64", "hex": value.hex()}
        elif value is not None and type(value) not in {str, int}:
            raise HeaderBoundsError("memory_scalar_invalid")
        values.append([name, value])
    try:
        encoded = json.dumps([MEMORY_VERSION, table, key, values], ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError) as error:
        raise HeaderBoundsError("memory_scalar_invalid") from error
    if len(encoded) > MAX_BYTES:
        raise HeaderBoundsError("memory_encoding_bound")
    return encoded


def memory_row_digest(table, key, row):
    return hashlib.sha256(b"seraph-memory-retained-row-v1\0" + encode_memory_row(table, key, row)).hexdigest()


@dataclass(frozen=True)
class BoundedMemoryRows:
    references: tuple[tuple[str, str], ...]
    rows: tuple[dict, ...]
    digests: tuple[str, ...]
    encoded_bytes: int
    certified_upper_bytes: int


async def read_memory_rows(db, references, *, remaining_bytes, existing_references=(), reserved_bytes=0):
    """Certify ALL selected headers before materializing any private row.

    The original source owner supplies the remainder after charging core/v3,
    protected metadata, all duplicated appearances and its Unknown reserve.
    These values are resource accounting inputs, never an authorization token.
    """
    if (type(references) is not tuple or type(existing_references) is not tuple
            or len(references) > MAX_ROWS or len(existing_references) > MAX_ROWS
            or type(reserved_bytes) is not int or reserved_bytes < 0
            or type(remaining_bytes) is not int or not 0 <= remaining_bytes <= MAX_BYTES):
        raise HeaderBoundsError("memory_closure_request_invalid")
    for reference in (*references, *existing_references):
        if (type(reference) is not tuple or len(reference) != 2
                or any(type(part) is not str for part in reference)):
            raise HeaderBoundsError("memory_closure_reference_invalid")
    selected = tuple(sorted(set(references)))
    if len(set((*selected, *existing_references))) > MAX_ROWS:
        raise HeaderBoundsError("memory_closure_reference_bound")
    remainder = remaining_bytes - reserved_bytes
    if remainder < 0:
        raise HeaderBoundsError("canonical_bound_not_certified")
    from src.memory.retention_schema import validate_memory_schema
    await validate_memory_schema(db)
    certificates = []
    upper_bytes = 0
    for table in sorted({table for table, _ in selected}):
        descriptor = MEMORY_DESCRIPTORS.get(table)
        if descriptor is None:
            raise HeaderBoundsError("memory_closure_reference_invalid")
        identities = tuple(key for current_table, key in selected if current_table == table)
        certificate = await preflight_exact_rows(db, descriptor, identities, remainder - upper_bytes)
        certificates.append(certificate)
        upper_bytes += certificate.upper_bytes
    rows, digests = [], []
    encoded_bytes = 0
    # No new write may occur between these headers and their actual row reads.
    for certificate in certificates:
        await validate_certificate(db, certificate)
        descriptor = certificate.descriptor
        columns = ",".join('"' + name + '"' for name in descriptor.columns)
        query = text(f'SELECT {columns} FROM "{descriptor.table}" WHERE "{descriptor.key}" COLLATE BINARY=:key LIMIT 2')
        for identity, upper in zip(certificate.row_ids, certificate.row_upper_bytes):
            await validate_certificate(db, certificate)
            matches = list(await db.execute(query, {"key": identity}))
            if len(matches) != 1:
                raise HeaderBoundsError("memory_closure_row_changed")
            row = dict(zip(descriptor.columns, matches[0]))
            encoded = encode_memory_row(descriptor.table, identity, row)
            if len(encoded) > upper:
                raise HeaderBoundsError("memory_header_bound_invalid")
            encoded_bytes += len(encoded)
            if encoded_bytes > remainder:
                raise HeaderBoundsError("memory_encoding_bound")
            rows.append(row)
            digests.append(hashlib.sha256(b"seraph-memory-retained-row-v1\0" + encoded).hexdigest())
    return BoundedMemoryRows(selected, tuple(rows), tuple(digests), encoded_bytes, upper_bytes)
