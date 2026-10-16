"""Dependency-free encoding shared by the runtime and managed lifecycle CLI."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from src.workspace.production import ProductionWorkspace, ProductionWorkspaceReconciliationError, read_lifecycle_receipt, LIFECYCLE_PATH_ENV, CANONICAL_CONTAINER_WORKSPACE, BIND_IDENTITY_ENV


def utc_period(now=None):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def period_state(account, rows, period):
    history = json.loads(account["settings_history_json"])
    authorized = datetime.fromisoformat(account["created_at"]).strftime("%Y-%m")
    reviewed = False
    periods = [authorized, *[row["period_id"] for row in rows]]
    for entry in history:
        if entry.get("kind") in {"period_initialized", "period_review"}:
            authorized = entry["period_id"]
            reviewed = entry.get("kind") == "period_review"
        if entry.get("kind") in {"period_initialized", "period_review", "period_observed"}:
            periods.append(entry["period_id"])
    high_water = max(periods)
    reason = ("accounting_clock_correction_required" if period < high_water and (period != authorized or not reviewed)
        else "accounting_period_review_required" if period != authorized else None)
    return {"authorized_period": authorized, "period_high_water": high_water, "reason_code": reason}


def unreviewed_overruns(account, rows):
    covered = set()
    for entry in json.loads(account["settings_history_json"]):
        if entry.get("kind") == "request_reserve_review":
            covered.update((row["operation_id"], row["sequence"], row["revision"]) for row in entry.get("operations", []))
    return [row for row in rows if row["state"] == "settled" and (row["actual_cost_microusd"] or 0) > row["bound_microusd"]
        and (row["operation_id"], row["sequence"], row["revision"]) not in covered]


def assert_deployment_binding(workspace):
    directory = workspace.lifecycle_directory
    if (not os.environ.get(LIFECYCLE_PATH_ENV) and workspace.lifecycle_path is None
        or not directory.is_absolute() or directory.resolve() != directory or directory.is_relative_to(workspace.host_root)):
        raise ProductionWorkspaceReconciliationError("accounting_continuity_unavailable")
    receipt = read_lifecycle_receipt(workspace) or {}
    binding = receipt.get("deployment_binding")
    expected = (os.environ.get(BIND_IDENTITY_ENV) if workspace.host_root == Path(CANONICAL_CONTAINER_WORKSPACE)
        else workspace.identity_digest)
    field = "host_bind_identity" if workspace.host_root == Path(CANONICAL_CONTAINER_WORKSPACE) else "root_path_digest"
    if not isinstance(binding, dict) or not expected or binding.get(field) != expected:
        raise ProductionWorkspaceReconciliationError("accounting_deployment_binding_unavailable")
    return receipt


# Ledger digest v2 (#1086). Each reservation's content hashes to a private,
# derived ``ledger_row_digest``; the account digest covers the account record
# (including its running totals) and the ordered root of those row digests.
# Everything stays recomputable from DB content alone, so maintenance tools
# fully re-verify any generation. v1 digests (no prefix) remain verifiable;
# the runtime migrates them forward. v2 is forward-only: older builds see a
# ``v2:`` digest as a continuity failure and fail closed.
LEDGER_V2_PREFIX = "v2:"
ROW_DIGEST_COLUMN = "ledger_row_digest"
TOTALS_COLUMN = "ledger_totals_json"
_DATETIME_FIELDS = ("created_at", "updated_at", "deadline_at", "contact_started_at")
_OPEN_STATES = ("reserved", "contact_started", "unknown")
_OVERRUN_FIELDS = ("operation_id", "sequence", "revision", "actual_cost_microusd", "bound_microusd",
    "job_id", "runtime_path", "period_id")


def ledger_record(values):
    record = dict(values)
    # Canonical writers own these unhashed private projections (a lookup hint
    # and the derived row digest). Raw SQLite reads and archived checkpoints
    # retain the existing financial wire.
    record.pop("group_lookup_key", None)
    record.pop(ROW_DIGEST_COLUMN, None)
    for field in _DATETIME_FIELDS:
        value = record.get(field)
        if isinstance(value, str):
            record[field] = datetime.fromisoformat(value).isoformat()
        elif isinstance(value, datetime):
            record[field] = value.isoformat()
    return record


def _v2_record(values):
    record = dict(values)
    record.pop("group_lookup_key", None)
    record.pop(ROW_DIGEST_COLUMN, None)
    for field in _DATETIME_FIELDS:
        value = record.get(field)
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                value = value.astimezone(timezone.utc).replace(tzinfo=None)
            record[field] = value.isoformat()
    return record


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def is_v2_digest(digest):
    return isinstance(digest, str) and digest.startswith(LEDGER_V2_PREFIX)


_ROW_DIGEST = re.compile(r"[0-9a-f]{64}")
_HEX = re.compile(r"[0-9a-f]*")


def valid_row_digest(value):
    return isinstance(value, str) and _ROW_DIGEST.fullmatch(value) is not None


def _joined_row_digests(digests):
    """Concatenate validated digests; one C-level hex check for the whole ledger."""
    if not all(type(digest) is str and len(digest) == 64 for digest in digests):
        return None
    joined = "".join(digests)
    return joined if _HEX.fullmatch(joined) is not None else None


def valid_row_digests(digests):
    return _joined_row_digests(list(digests)) is not None


def row_digest(values):
    """Digest of one reservation's financial content (private columns excluded)."""
    return hashlib.sha256(_canonical(_v2_record(values)).encode()).hexdigest()


def rows_root(digests_in_order):
    """Root over row digests in ``operation_id`` order (BINARY collation)."""
    digests = list(digests_in_order)
    joined = _joined_row_digests(digests)
    if joined is None:
        raise ProductionWorkspaceReconciliationError("accounting row digest invalid")
    return rows_root_from_joined(joined, len(digests))


def rows_root_from_joined(joined, count):
    """``rows_root`` for an already validated concatenation of ``count`` digests."""
    root = hashlib.sha256(joined.encode())
    root.update(f":{count}".encode())
    return root.hexdigest()


def v2_ledger_digest(account, root):
    owner = _v2_record(account)
    owner.pop("ledger_digest", None)
    return LEDGER_V2_PREFIX + hashlib.sha256(_canonical({"account": owner, "rows_root": root}).encode()).hexdigest()


def _v1_ledger_digest(account, rows):
    owner = ledger_record(account)
    owner.pop("ledger_digest", None)
    owner.pop(TOTALS_COLUMN, None)
    payload = {"account": owner, "operations": [ledger_record(row) for row in sorted(rows, key=lambda row: row["operation_id"])]}
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def ledger_digest(account, rows):
    """Recompute the account digest from content, in the scheme the account uses."""
    if not is_v2_digest(account.get("ledger_digest")):
        return _v1_ledger_digest(account, rows)
    ordered = sorted(rows, key=lambda row: row["operation_id"])
    return v2_ledger_digest(account, rows_root(row_digest(row) for row in ordered))


def stored_row_digest_mismatch(rows):
    """Raw rows whose stored derived digest disagrees with their content."""
    return [row["operation_id"] for row in rows
        if row.get(ROW_DIGEST_COLUMN) is not None and row.get(ROW_DIGEST_COLUMN) != row_digest(row)]


def empty_ledger_totals():
    # The scoped counts let a writer prove that an indexed read returned every
    # row it must see: a row hidden by editing its owner, group key or state
    # changes the read but not these sealed counts.
    return {"schema_version": 1, "open_bound_by_state": {state: 0 for state in _OPEN_STATES},
        "owner_open_bound": {}, "settled_by_period": {}, "owner_settled_by_period": {},
        "overruns": {}, "rows_by_period": {}, "row_count": 0,
        "rows_by_owner": {}, "rows_by_group": {}, "owner_near_text_open": {}}


_GROUP_EVIDENCE_KIND = "general_task_group_reservation.v1"
_NEAR_TEXT_RUNTIME = "near_text_native"


def evidence_group_key(evidence_json):
    """The private lookup key a valid row's group evidence classifies to.

    Dependency-free and deliberately lenient: full verification separately
    rejects malformed evidence and any stored key that disagrees with it.
    """
    try:
        entries = json.loads(evidence_json or "[]")
    except (TypeError, ValueError):
        return None
    found = [entry for entry in entries if isinstance(entry, dict) and entry.get("kind") == _GROUP_EVIDENCE_KIND] \
        if isinstance(entries, list) else []
    group = found[0].get("group") if len(found) == 1 else None
    group_id = group.get("group_id") if isinstance(group, dict) else None
    return "g:" + group_id if isinstance(group_id, str) else None


def _bump(mapping, key, sign):
    mapping[key] = mapping.get(key, 0) + sign
    if mapping[key] == 0:
        del mapping[key]


def apply_ledger_contribution(totals, row, sign):
    """Add (sign=1) or remove (sign=-1) one reservation's held amounts."""
    state, owner = row["state"], row["owner_id"]
    if state in _OPEN_STATES:
        bound = int(row["bound_microusd"])
        totals["open_bound_by_state"][state] += sign * bound
        totals["owner_open_bound"][owner] = totals["owner_open_bound"].get(owner, 0) + sign * bound
    elif state == "settled":
        actual, period = int(row["actual_cost_microusd"] or 0), row["period_id"]
        totals["settled_by_period"][period] = totals["settled_by_period"].get(period, 0) + sign * actual
        by_owner = totals["owner_settled_by_period"].setdefault(owner, {})
        by_owner[period] = by_owner.get(period, 0) + sign * actual
        if actual > int(row["bound_microusd"]):
            if sign > 0:
                totals["overruns"][row["operation_id"]] = {field: row[field] for field in _OVERRUN_FIELDS}
            else:
                totals["overruns"].pop(row["operation_id"], None)
    totals["row_count"] += sign
    period = row["period_id"]
    totals["rows_by_period"][period] = totals["rows_by_period"].get(period, 0) + sign
    _bump(totals["rows_by_owner"], owner, sign)
    group_key = evidence_group_key(row.get("evidence_json"))
    if group_key is not None:
        _bump(totals["rows_by_group"], group_key, sign)
    if state in _OPEN_STATES and row.get("runtime_path") == _NEAR_TEXT_RUNTIME:
        _bump(totals["owner_near_text_open"], owner, sign)
    _drop_zeros(totals)


def _drop_zeros(totals):
    for period in [period for period, value in totals["rows_by_period"].items() if value == 0]:
        del totals["rows_by_period"][period]
    for owner in [owner for owner, value in totals["owner_open_bound"].items() if value == 0]:
        del totals["owner_open_bound"][owner]
    for period in [period for period, value in totals["settled_by_period"].items() if value == 0]:
        del totals["settled_by_period"][period]
    for owner, periods in list(totals["owner_settled_by_period"].items()):
        for period in [period for period, value in periods.items() if value == 0]:
            del periods[period]
        if not periods:
            del totals["owner_settled_by_period"][owner]


def ledger_totals(rows):
    """Running totals recomputed from every row (the full-verification oracle)."""
    totals = empty_ledger_totals()
    for row in rows:
        apply_ledger_contribution(totals, ledger_record(row), 1)
    return totals


def held_microusd(totals, period, owner=None):
    """Open liabilities plus settlements at or after ``period`` (``held()``)."""
    if owner is None:
        open_bound = sum(totals["open_bound_by_state"].values())
        settled = totals["settled_by_period"]
    else:
        open_bound = totals["owner_open_bound"].get(owner, 0)
        settled = totals["owner_settled_by_period"].get(owner, {})
    return open_bound + sum(value for key, value in settled.items() if key >= period)


def totals_period_rows(totals):
    """The only row input ``period_state`` reads: the highest row period."""
    return [{"period_id": max(totals["rows_by_period"])}] if totals["rows_by_period"] else []


def totals_overrun_rows(totals):
    """The only rows ``unreviewed_overruns`` can return: settled overruns."""
    return [{**row, "state": "settled"} for row in totals["overruns"].values()]


def witness(account):
    return {field: account[field] for field in ("deployment_id", "revision", "ledger_digest")}


@contextmanager
def maintenance_accounting_lock(root, *, require_binding=True):
    workspace = root if isinstance(root, ProductionWorkspace) else ProductionWorkspace(host_root=root)
    if require_binding:
        assert_deployment_binding(workspace)
    directory = workspace.lifecycle_directory
    if directory.is_symlink() or not directory.is_dir():
        raise ProductionWorkspaceReconciliationError("accounting continuity unavailable")
    descriptor = os.open(directory / "accounting.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProductionWorkspaceReconciliationError("accounting continuity busy") from exc
        yield workspace
    finally:
        os.close(descriptor)


def configuration_digest(payload):
    normalized = dict(payload)
    normalized.pop("updated_at", None)
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _assert_credential_free_configuration(payload):
    # This dependency-free path also reads archived JSON. Do not trust the
    # runtime validator to have validated a restored file before retaining it.
    from urllib.parse import urlsplit
    def inspect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "").replace("_", "")
                if normalized in {"apikey", "authorization", "password", "secret", "token", "accesstoken", "refreshtoken"}:
                    raise ProductionWorkspaceReconciliationError("provider policy inline credential unavailable")
                if key in {"endpoint", "base_url", "api_base"} and isinstance(item, str) and urlsplit(item).username is not None:
                    raise ProductionWorkspaceReconciliationError("provider policy endpoint credential unavailable")
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
    if not isinstance(payload, dict):
        raise ProductionWorkspaceReconciliationError("provider policy configuration unavailable")
    inspect(payload)
    near = payload.get("near_text")
    if near is not None and (not isinstance(near, dict) or near.get("api_base") != "https://cloud-api.near.ai/v1"):
        raise ProductionWorkspaceReconciliationError("provider policy endpoint unavailable")


def policy_continuity(workspace, payload):
    receipt = assert_deployment_binding(workspace)
    record = receipt.get("provider_policy")
    if (not isinstance(record, dict) or record.get("revision") != payload.get("egress_revision", 1)
        or record.get("configuration_digest") != configuration_digest(payload)):
        return False, record
    return True, record


def verify_restored_policy_reconciliation(*, root, archived, staged):
    """Permit only the owning, witnessed revocation of archived authority."""
    original, current = json.loads(archived), json.loads(staged)
    if not isinstance(original, dict) or not isinstance(current, dict):
        return False
    if (current.get("egress_revoked") is not True or current.get("egress_revocation_key") is not None
        or type(current.get("egress_revision")) is not int
        or current["egress_revision"] <= original.get("egress_revision", 1)):
        return False
    expected = {**original, "egress_revoked": True, "egress_revocation_key": None,
        "egress_revision": current["egress_revision"]}
    return current == expected and policy_continuity(ProductionWorkspace(host_root=root), current)[0]


def _write_configuration_file(path, payload):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class PolicyRevisionConflict(RuntimeError):
    pass


def _publish_policy_locked(workspace, payload, *, target_path, expected_revision=None):
    from src.workspace.production import write_lifecycle_receipt, _write_private_checkpoint
    _assert_credential_free_configuration(payload)
    receipt = read_lifecycle_receipt(workspace) or {}
    prior = receipt.get("provider_policy")
    if expected_revision is not None:
        if type(expected_revision) is not int or expected_revision < 1:
            raise PolicyRevisionConflict("provider_policy_revision_changed")
        current = json.loads(target_path.read_text()) if target_path.is_file() else None
        empty_bootstrap = current is None and isinstance(prior, dict) and prior.get("revision") == 0 and expected_revision == 1
        if not empty_bootstrap and (not isinstance(current, dict) or current.get("egress_revision", 1) != expected_revision or not policy_continuity(workspace, current)[0]):
            raise PolicyRevisionConflict("provider_policy_revision_changed")
    revision = payload.get("egress_revision", 1)
    record = {"revision": revision, "configuration_digest": configuration_digest(payload),
        "state": "revoked" if payload.get("egress_revoked") else "active"}
    if (not isinstance(prior, dict) or type(revision) is not int
        or revision < prior["revision"] or revision == prior["revision"] and record != prior):
        raise ProductionWorkspaceReconciliationError("provider_policy_revision_changed")
    checkpoint = {"schema_version": 1, "base": prior, "target": record, "configuration": payload,
        "secret_values_included": False}
    _write_private_checkpoint(workspace.lifecycle_directory / "provider-policy-checkpoint.json", checkpoint)
    receipt["provider_policy"] = record
    write_lifecycle_receipt(workspace, receipt, _accounting_lock_held=True)
    _write_configuration_file(target_path, payload)
    return record


def publish_policy_configuration(root, payload, *, expected_revision=None):
    with maintenance_accounting_lock(root) as workspace:
        return _publish_policy_locked(workspace, payload, target_path=root / "model-fabric-settings.json", expected_revision=expected_revision)


def revoke_restored_policy(*, active, target):
    """Only the existing fenced lifecycle calls this staged publication."""
    from src.workspace.lifecycle import _require_production_fence
    from src.workspace import canonical_workspace_registry
    _require_production_fence(canonical_workspace_registry(active.host_root))
    existing = read_lifecycle_receipt(active) or {}
    candidate = target / "model-fabric-settings.json"
    if not existing.get("provider_policy", {}).get("revision", 0) and candidate.is_file() and not candidate.is_symlink():
        contents = json.loads(candidate.read_text())
        if not isinstance(contents, dict) or not (contents.get("openrouter_setup") or contents.get("near_text")):
            return {"state": "uninitialized"}
    with maintenance_accounting_lock(active) as workspace:
        receipt = read_lifecycle_receipt(workspace) or {}
        path = target / "model-fabric-settings.json"
        if not path.is_file() or path.is_symlink():
            if receipt.get("provider_policy", {}).get("revision", 0):
                raise ProductionWorkspaceReconciliationError("provider policy restore configuration unavailable")
            return {"state": "uninitialized"}
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict) or not (payload.get("openrouter_setup") or payload.get("near_text")):
            if receipt.get("provider_policy", {}).get("revision", 0):
                raise ProductionWorkspaceReconciliationError("provider policy restore configuration unavailable")
            return {"state": "uninitialized"}
        payload.update(egress_revoked=True, egress_revocation_key=None,
            egress_revision=max(receipt.get("provider_policy", {}).get("revision", 0), payload.get("egress_revision", 1)) + 1)
        return _publish_policy_locked(workspace, payload, target_path=path)


def reconcile_policy_checkpoint(root):
    from src.workspace.production import _read_private_checkpoint
    from src.workspace.lifecycle import _require_production_fence
    from src.workspace import canonical_workspace_registry
    _require_production_fence(canonical_workspace_registry(root))
    with maintenance_accounting_lock(root) as workspace:
        receipt = read_lifecycle_receipt(workspace) or {}
        checkpoint = _read_private_checkpoint(workspace.lifecycle_directory / "provider-policy-checkpoint.json")
        if checkpoint is None or checkpoint.get("target") != receipt.get("provider_policy"):
            raise ProductionWorkspaceReconciliationError("provider policy checkpoint unavailable")
        payload = checkpoint["configuration"]
        if configuration_digest(payload) != checkpoint["target"]["configuration_digest"]:
            raise ProductionWorkspaceReconciliationError("provider policy checkpoint digest mismatch")
        path = root / "model-fabric-settings.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        if configuration_digest(current) == checkpoint["target"]["configuration_digest"]:
            return {"status": "already_reconciled", "revision": checkpoint["target"]["revision"]}
        # Completing an interrupted active write must never activate a grant.
        if not payload.get("egress_revoked"):
            payload = {**payload, "egress_revoked": True, "egress_revocation_key": None,
                "egress_revision": checkpoint["target"]["revision"] + 1}
        result = _publish_policy_locked(workspace, payload, target_path=path)
        return {"status": "reconciled_revoked", "revision": result["revision"], "job_authority_changed": False}
