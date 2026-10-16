"""Bounded verification for the inference cost ledger (#1086).

Every accounting writer used to load, re-validate and re-hash the whole ledger
under ``BEGIN IMMEDIATE``, so each writer-lock hold grew with ledger size.
Ledger digest v2 (``src.workspace.accounting_witness``) makes the check
incremental without weakening what admission trusts:

* **Fast verification** (every accounting writer and reader, #1095). The external witness
  must equal the account witness. The ordered root of the stored row digests
  plus the account record (including its running totals) must reproduce the
  account digest. Every row the operation reads to decide, or modifies, must
  hash to its stored digest. Admission reads only the running totals, so
  editing an untouched row can neither free capacity nor clear the overrun
  latch.
* **Full verification** (first writer per process and deployment, any writer
  once the last full pass is older than ``2 * FULL_VERIFICATION_INTERVAL``,
  the background verifier, which startup recovery runs first off the writer
  lock, and the maintenance tools). It re-validates every row's evidence and group key,
  recomputes every row digest from content and the totals from every row.

Scoped reads select rows by unhashed or content columns (the private
``group_lookup_key``, owner, job). Each must return exactly the count sealed in
the totals for its scope (per group key, discovery-service rows), the open set
read by recovery and research cancel must sum to the sealed open liabilities
per state, and the near-text outstanding check reads its sealed per-owner count
instead of a query, so a row hidden by editing an untouched row's columns is
refused rather than under-counted. The programme budget takes each row's
programme from its digest-covered evidence, not the unsealed run row. A content
edit to a row no operation reads is caught at the next full verification and
cannot change any admission decision.

Accepted residual: consumer reads outside the accounting writer path
(guardian opportunity runtime, research snapshot and coordinator close-out,
repository source recovery) read reservations without digest checks. They
classify job or display state only and never admit spend or free capacity; a
row hidden from them stays held in the sealed totals and is caught at the next
full verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import bisect
import json
import logging
import time
from typing import Any

from sqlalchemy import inspect, text

from src.db.models import InferenceAccountingOwner, InferenceCostReservation
from src.workspace import accounting_witness as witness_codec

logger = logging.getLogger(__name__)

FULL_VERIFICATION_INTERVAL_SECONDS = 600.0
_STALE_AFTER_SECONDS = 2 * FULL_VERIFICATION_INTERVAL_SECONDS
_DERIVED_COLUMNS = frozenset({"group_lookup_key", witness_codec.ROW_DIGEST_COLUMN})
_SESSION_KEY = "inference_ledger_state"

# Process-local verification state per (workspace root, deployment id). A
# fresh process has no entry, so its first writer always verifies in full.
_verified_at: dict[tuple[str, str], float] = {}
_latched: dict[tuple[str, str], str] = {}
# Test-only: also compute every hot-path decision from the full ledger with
# the pre-#1086 code and fail on any difference.
SHADOW_FULL_LEDGER = False


V1_LEDGER_SKIPPED = "v1_ledger_skipped"


class LedgerVerificationError(Exception):
    """A continuity failure detected by ledger verification."""


class DigestIndex:
    """Stored row digests in ``operation_id`` order, plus their concatenation.

    The root is the hash of that concatenation, so a persist only splices the
    changed digests in instead of re-sorting and re-joining the ledger.
    """

    def __init__(self, order: list[str], digests: list[str], joined: str):
        self._order, self._digests, self._joined = order, digests, joined

    @classmethod
    def build(cls, pairs) -> "DigestIndex | None":
        pairs = list(pairs)
        order = [operation_id for operation_id, _ in pairs]
        digests = [digest for _, digest in pairs]
        joined = witness_codec._joined_row_digests(digests)
        # operation_id is the primary key, so sorted order is also unique.
        if joined is None or order != sorted(order):
            return None
        return cls(order, digests, joined)

    def _position(self, operation_id: str) -> int:
        return bisect.bisect_left(self._order, operation_id)

    def get(self, operation_id: str) -> str | None:
        position = self._position(operation_id)
        if position < len(self._order) and self._order[position] == operation_id:
            return self._digests[position]
        return None

    def __contains__(self, operation_id: str) -> bool:
        return self.get(operation_id) is not None

    def __getitem__(self, operation_id: str) -> str:
        digest = self.get(operation_id)
        if digest is None:
            raise KeyError(operation_id)
        return digest

    def __setitem__(self, operation_id: str, digest: str) -> None:
        if not witness_codec.valid_row_digest(digest):
            raise LedgerVerificationError("ledger_row_digest_invalid")
        position = self._position(operation_id)
        start = position * 64
        if position < len(self._order) and self._order[position] == operation_id:
            self._digests[position] = digest
            self._joined = self._joined[:start] + digest + self._joined[start + 64:]
        else:
            self._order.insert(position, operation_id)
            self._digests.insert(position, digest)
            self._joined = self._joined[:start] + digest + self._joined[start:]

    def __len__(self) -> int:
        return len(self._order)

    def root(self) -> str:
        return witness_codec.rows_root_from_joined(self._joined, len(self._order))


@dataclass
class LedgerState:
    """What one transaction verified, reused by its persist."""

    digests: DigestIndex
    totals: dict[str, Any]
    full: bool
    rows: list[InferenceCostReservation] | None = None
    # Verified content of every row read or about to change (R2), keyed by
    # operation id; the persist compares each pre-image against ``digests``.
    pre_images: dict[str, dict[str, Any]] = field(default_factory=dict)
    # v1 ledger or rows without a stored digest: the persist seals every row.
    backfill: bool = False


def _key(workspace, account) -> tuple[str, str]:
    return (str(workspace.host_root), str(account.deployment_id))


def latched_reason(workspace, account) -> str | None:
    return _latched.get(_key(workspace, account))


def latch(workspace, account, reason: str) -> None:
    logger.error("inference accounting ledger verification failed (%s); accounting is blocked until restart", reason)
    _latched[_key(workspace, account)] = reason


def record_full_verification(workspace, account) -> None:
    _verified_at[_key(workspace, account)] = time.monotonic()


def full_verification_due(workspace, account) -> bool:
    last = _verified_at.get(_key(workspace, account))
    return last is None or time.monotonic() - last > _STALE_AFTER_SECONDS


def reset_process_state() -> None:
    _verified_at.clear()
    _latched.clear()


def row_values(row: InferenceCostReservation) -> dict[str, Any]:
    return row.model_dump(mode="json", exclude=set(_DERIVED_COLUMNS))


def _pre_image(row: InferenceCostReservation) -> dict[str, Any]:
    state = inspect(row)
    values = row_values(row)
    for attribute in state.mapper.column_attrs:
        name = attribute.key
        if name in _DERIVED_COLUMNS:
            continue
        history = state.attrs[name].history
        if history.deleted:
            original = history.deleted[0]
            values[name] = original.isoformat() if hasattr(original, "isoformat") else original
    return values


def _content_changed(row: InferenceCostReservation) -> bool:
    state = inspect(row)
    if state.pending:
        return True
    return any(state.attrs[attribute.key].history.has_changes()
        for attribute in state.mapper.column_attrs if attribute.key not in _DERIVED_COLUMNS)


async def scan_row_digests(db) -> tuple[DigestIndex | None, bool]:
    """Stored row digests in ``operation_id`` order; False if any is unusable."""
    # Index-only scan of (operation_id, ledger_row_digest). Plain driver
    # tuples: building 10k SQLAlchemy rows would dominate the writer hold.
    query = "SELECT operation_id, ledger_row_digest FROM inference_cost_reservations ORDER BY operation_id"
    fetched = None
    connection_factory = getattr(db, "connection", None)
    if connection_factory is not None:
        connection = await connection_factory()
        raw = await connection.get_raw_connection()
        driver = getattr(raw, "driver_connection", None)
        if driver is not None and hasattr(driver, "execute"):
            cursor = await driver.execute(query)
            fetched = await cursor.fetchall()
            await cursor.close()
    if fetched is None:
        fetched = (await db.execute(text(query))).fetchall()
    index = DigestIndex.build(fetched)
    return index, index is not None


def _root(digests: DigestIndex) -> str:
    return digests.root()


def parse_totals(account) -> dict[str, Any]:
    try:
        totals = json.loads(account.ledger_totals_json)
    except (TypeError, ValueError) as exc:
        raise LedgerVerificationError("ledger_totals_unavailable") from exc
    if not isinstance(totals, dict) or totals.get("schema_version") != 1:
        raise LedgerVerificationError("ledger_totals_unavailable")
    return totals


def verify_fast(account, receipt_witness, digests: DigestIndex) -> dict[str, Any]:
    """Witness + root + account record; returns the trusted running totals."""
    from src.workflows.inference_accounting import _witness
    if receipt_witness != _witness(account):
        raise LedgerVerificationError("ledger_witness_mismatch")
    if not witness_codec.is_v2_digest(account.ledger_digest):
        raise LedgerVerificationError("ledger_digest_v1")
    expected = witness_codec.v2_ledger_digest(account.model_dump(mode="json"), _root(digests))
    if expected != account.ledger_digest:
        raise LedgerVerificationError("ledger_root_mismatch")
    totals = parse_totals(account)
    if totals.get("row_count") != len(digests):
        raise LedgerVerificationError("ledger_totals_mismatch")
    return totals


def verify_full(account, rows, receipt_witness) -> dict[str, Any]:
    """``receipt_witness`` may be a callable, read only after row evidence passes."""
    from src.workflows.general_task_accounting import entry_for
    from src.workflows.inference_accounting import _witness
    from src.workflows.inference_group_lookup import assert_group_lookup
    # Original evidence failures take precedence over derived lookup drift,
    # including drift on an earlier reservation in this complete ledger.
    for row in rows:
        entry_for(row)
    for row in rows:
        assert_group_lookup(row)
    if callable(receipt_witness):
        receipt_witness = receipt_witness()
    if account is None or receipt_witness != _witness(account):
        raise LedgerVerificationError("ledger_witness_mismatch")
    values = [row_values(row) for row in rows]
    if witness_codec.ledger_digest(account.model_dump(mode="json"), values) != account.ledger_digest:
        raise LedgerVerificationError("ledger_digest_mismatch")
    for row, value in zip(rows, values):
        if row.ledger_row_digest is not None and row.ledger_row_digest != witness_codec.row_digest(value):
            raise LedgerVerificationError("ledger_row_digest_mismatch")
    recomputed = witness_codec.ledger_totals(values)
    if witness_codec.is_v2_digest(account.ledger_digest):
        if parse_totals(account) != recomputed:
            raise LedgerVerificationError("ledger_totals_mismatch")
    return recomputed


def verify_loaded_rows(state: LedgerState, rows) -> None:
    """R2: a row read to decide or about to change must match its digest."""
    for row in rows:
        if row is None or row.operation_id in state.pre_images:
            continue
        values = row_values(row)
        stored = state.digests.get(row.operation_id)
        if stored is None or stored != witness_codec.row_digest(values):
            raise LedgerVerificationError("ledger_row_digest_mismatch")
        state.pre_images[row.operation_id] = values


async def persist(db, workspace, account) -> list[InferenceCostReservation]:
    """Seal this session's reservation changes; returns the content-changed rows.

    The changed set comes from the session (R3). Each modified row's verified
    pre-image must hash to the digest this transaction verified (R2); the
    running totals move by post-image minus pre-image here and only here.
    """
    from datetime import datetime, timezone
    from src.workflows.inference_group_lookup import assert_group_lookup, classify_group_lookup
    state: LedgerState | None = db.info.get(_SESSION_KEY)
    if state is None:
        raise LedgerVerificationError("ledger_state_unavailable")
    dirty = [item for item in (*db.new, *db.dirty) if isinstance(item, InferenceCostReservation)]
    if any(isinstance(item, InferenceAccountingOwner) and item is not account for item in (*db.new, *db.dirty)):
        raise LedgerVerificationError("ledger_owner_ambiguous")
    totals = state.totals
    changed = []
    for row in dirty:
        if inspect(row).pending and row.group_lookup_key is None:
            row.group_lookup_key = classify_group_lookup(row)
        assert_group_lookup(row)
        if not _content_changed(row):
            continue
        post = row_values(row)
        if inspect(row).pending:
            if row.operation_id in state.digests:
                raise LedgerVerificationError("ledger_operation_duplicate")
        else:
            pre = state.pre_images.get(row.operation_id) or _pre_image(row)
            if state.digests.get(row.operation_id) != witness_codec.row_digest(pre):
                raise LedgerVerificationError("ledger_row_digest_mismatch")
            witness_codec.apply_ledger_contribution(totals, witness_codec.ledger_record(pre), -1)
        witness_codec.apply_ledger_contribution(totals, witness_codec.ledger_record(post), 1)
        digest = witness_codec.row_digest(post)
        row.ledger_row_digest = digest
        state.digests[row.operation_id] = digest
        state.pre_images[row.operation_id] = post
        changed.append(row)
    if state.backfill:
        # Derived-only: sealing stored digests never changes financial content.
        for row in state.rows or ():
            if row.ledger_row_digest != state.digests[row.operation_id]:
                row.ledger_row_digest = state.digests[row.operation_id]
        state.backfill = False
    account.ledger_totals_json = json.dumps(totals, sort_keys=True, separators=(",", ":"))
    account.revision += 1
    # Normalize SQLite datetimes before hashing so reopen has identical
    # bytes. All timestamps describe UTC regardless of storage timezone.
    account.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.flush()
    account.ledger_digest = witness_codec.v2_ledger_digest(account.model_dump(mode="json"), _root(state.digests))
    db.add(account)
    await db.flush()
    if SHADOW_FULL_LEDGER:
        await _shadow_check_totals(db, account)
    return changed


def full_state(account, rows) -> LedgerState:
    """State for a transaction that loaded the whole ledger (verified separately)."""
    ordered = sorted(rows, key=lambda item: item.operation_id)
    values = {row.operation_id: row_values(row) for row in ordered}
    computed = {operation_id: witness_codec.row_digest(value) for operation_id, value in values.items()}
    digests = DigestIndex.build(computed.items())
    backfill = account is None or not witness_codec.is_v2_digest(account.ledger_digest) or any(
        row.ledger_row_digest != computed[row.operation_id] for row in ordered)
    return LedgerState(digests=digests, totals=witness_codec.ledger_totals(list(values.values())),
        full=True, rows=list(ordered), pre_images=values, backfill=backfill)


async def _shadow_check_totals(db, account) -> None:
    from sqlmodel import select
    rows = list((await db.execute(select(InferenceCostReservation))).scalars())
    recomputed = witness_codec.ledger_totals([row_values(row) for row in rows])
    if parse_totals(account) != recomputed:
        raise AssertionError(f"ledger totals drifted: {account.ledger_totals_json} != {recomputed}")
    for row in rows:
        if row.ledger_row_digest != witness_codec.row_digest(row_values(row)):
            raise AssertionError(f"ledger row digest drifted: {row.operation_id}")


def verify_offline(database_path) -> tuple[str | None, str | None, int | None]:
    """DB-internal full verification on a worker thread (stdlib sqlite3).

    Never compares the external witness: writers persist it before commit, so
    an unserialized reader can legitimately see it ahead (R6). Returns
    ``(deployment_id, failure_reason_or_None, revision)``.
    """
    import sqlite3
    from types import SimpleNamespace
    from src.workflows.general_task_accounting import entry_for
    from src.workflows.inference_group_lookup import assert_group_lookup
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        owners = connection.execute("SELECT * FROM inference_accounting_owners").fetchall()
        if len(owners) != 1:
            return None, None, None
        account = dict(owners[0])
        rows = [dict(row) for row in connection.execute("SELECT * FROM inference_cost_reservations ORDER BY operation_id")]
    finally:
        connection.close()
    deployment_id, revision = account["deployment_id"], account["revision"]
    if not witness_codec.is_v2_digest(account["ledger_digest"]):
        # Not verified here: a v1 ledger is fully checked by every writer
        # until startup recovery migrates it, so no pass may be recorded.
        return deployment_id, V1_LEDGER_SKIPPED, revision
    try:
        for row in rows:
            namespace = SimpleNamespace(**row)
            entry_for(namespace)
            assert_group_lookup(namespace)
        if witness_codec.ledger_digest(account, rows) != account["ledger_digest"]:
            return deployment_id, "ledger_digest_mismatch", revision
        if witness_codec.stored_row_digest_mismatch(rows) or any(row["ledger_row_digest"] is None for row in rows):
            return deployment_id, "ledger_row_digest_mismatch", revision
        if json.loads(account["ledger_totals_json"]) != witness_codec.ledger_totals(rows):
            return deployment_id, "ledger_totals_mismatch", revision
    except Exception as exc:  # any evidence/decoding failure is a verification failure
        return deployment_id, f"ledger_evidence_invalid:{type(exc).__name__}", revision
    return deployment_id, None, revision


async def shadow_compare_decisions(db, account, totals, period: str, owner_id: str | None) -> None:
    """Test-only oracle: the pre-#1086 full-ledger admission inputs (R1)."""
    if not SHADOW_FULL_LEDGER:
        return
    from sqlmodel import select
    from src.workspace import accounting_witness as codec
    rows = list((await db.execute(select(InferenceCostReservation))).scalars())
    operations = [row.model_dump(mode="json", exclude={"group_lookup_key", codec.ROW_DIGEST_COLUMN}) for row in rows]
    owner_data = account.model_dump(mode="json")

    def held(row):
        if row.state in {"reserved", "contact_started", "unknown"}:
            return row.bound_microusd
        return (row.actual_cost_microusd or 0) if row.period_id >= period and row.state == "settled" else 0

    expected = (
        codec.period_state(owner_data, operations, period),
        sorted(item["operation_id"] for item in codec.unreviewed_overruns(owner_data, operations)),
        sum(held(row) for row in rows),
        sum(held(row) for row in rows if row.owner_id == owner_id) if owner_id is not None else None,
    )
    actual = (
        codec.period_state(owner_data, codec.totals_period_rows(totals), period),
        sorted(item["operation_id"] for item in codec.unreviewed_overruns(owner_data, codec.totals_overrun_rows(totals))),
        codec.held_microusd(totals, period),
        codec.held_microusd(totals, period, owner_id) if owner_id is not None else None,
    )
    if expected != actual:
        raise AssertionError(f"ledger totals decision drift: {actual} != {expected}")


async def shadow_compare_helper(scoped, full) -> None:
    """Test-only oracle: a scoped helper call must equal its full-ledger call."""
    if not SHADOW_FULL_LEDGER:
        return

    async def outcome(call):
        try:
            return ("ok", await call())
        except Exception as exc:  # compare the denial, not the traceback
            return ("raised", type(exc).__name__, getattr(exc, "code", str(exc)))

    left, right = await outcome(scoped), await outcome(full)
    if left != right:
        raise AssertionError(f"scoped ledger helper drift: {left} != {right}")
