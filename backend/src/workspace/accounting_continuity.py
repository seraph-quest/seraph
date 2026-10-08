"""Retain the latest inference ledger during managed root promotion."""

from pathlib import Path
import sqlite3
import json
import hashlib
from datetime import datetime, timezone


# The same immutable invocation/authority binding checked by durable admission,
# plus the legacy row/root/conversation identity. Mutable execution receipts
# come from the verified canonical source, never a stale target generation.
_JOB_BINDING_FIELDS = (
    "id", "run_identity", "root_run_identity", "parent_run_identity", "workflow_name", "tool_name",
    "session_id", "conversation_id", "operator_session_id", "branch_kind", "branch_depth",
    "run_fingerprint", "arguments_json", "record_schema_version", "parent_job_id", "parent_fencing_token",
    "job_kind", "owner_kind", "owner_principal_id", "service_id", "goal_id", "goal_revision",
    "plan_revision", "candidate_id", "capability_version", "input_digest", "authority_digest",
    "budget_digest", "idempotency_scope", "idempotency_key", "idempotency_binding", "priority",
    "dependencies_json", "resource_claims_json", "declared_authority_json", "deadline_at", "max_attempts",
)
_JOB_RECONCILIATION_KEY = "inference_accounting_restore_reconciliation"


def _retained_job_values(source, target, account):
    from src.workspace.production import ProductionWorkspaceReconciliationError
    values = dict(source)
    prior = dict(target) if target is not None else None
    if any(field not in values for field in _JOB_BINDING_FIELDS):
        raise ProductionWorkspaceReconciliationError("accounting canonical job binding schema unavailable")
    if prior is not None and any(prior.get(field) != values[field] for field in _JOB_BINDING_FIELDS):
        raise ProductionWorkspaceReconciliationError("accounting canonical job binding mismatch")
    for row in (values, prior):
        if row is not None and any(type(row.get(field)) is not int or row[field] < 0 for field in ("revision", "fencing_token")):
            raise ProductionWorkspaceReconciliationError("accounting canonical job fence invalid")
    try:
        metadata = json.loads(values["metadata_json"]) if values["metadata_json"] is not None else {}
        prior_metadata = json.loads(prior["metadata_json"]) if prior is not None and prior["metadata_json"] is not None else {}
    except (TypeError, ValueError) as exc:
        raise ProductionWorkspaceReconciliationError("accounting canonical job metadata unavailable") from exc
    if not isinstance(metadata, dict) or not isinstance(prior_metadata, dict):
        raise ProductionWorkspaceReconciliationError("accounting canonical job metadata unavailable")
    source_digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    binding = {"deployment_id": account["deployment_id"], "accounting_revision": account["revision"],
        "source_job_digest": source_digest}
    values.update(status="blocked", failure_reason="workspace_restore_requires_reconciliation",
        lease_owner=None, lease_expires_at=None)
    marker = prior_metadata.get(_JOB_RECONCILIATION_KEY)
    if prior is not None and isinstance(marker, dict):
        expected_marker = {**binding, "revision": prior["revision"], "fencing_token": prior["fencing_token"]}
        expected_metadata = {**metadata, _JOB_RECONCILIATION_KEY: expected_marker}
        mutable = {"revision", "fencing_token", "metadata_json", "updated_at"}
        if (marker == expected_marker and prior_metadata == expected_metadata
            and all(prior.get(field) == value for field, value in values.items() if field not in mutable)
            and prior["revision"] > source["revision"] and prior["fencing_token"] > source["fencing_token"]):
            # Exact retry after retention but before descriptor promotion.
            # The previous fresh fence remains authoritative; no churn.
            return prior
    values["revision"] = max(values["revision"], prior["revision"] if prior else -1) + 1
    values["fencing_token"] = max(values["fencing_token"], prior["fencing_token"] if prior else -1) + 1
    values["metadata_json"] = json.dumps({**metadata, _JOB_RECONCILIATION_KEY: {**binding,
        "revision": values["revision"], "fencing_token": values["fencing_token"]}}, sort_keys=True, separators=(",", ":"))
    values["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    return values


def verify_accounting_generation(database, expected):
    from src.workspace.accounting_witness import ledger_record, ledger_digest, witness
    from src.workspace.production import ProductionWorkspaceReconciliationError
    if not database.is_file() or database.is_symlink():
        raise ProductionWorkspaceReconciliationError("accounting continuity database unavailable")
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        owners = db.execute("SELECT * FROM inference_accounting_owners").fetchall()
        if len(owners) != 1:
            raise ProductionWorkspaceReconciliationError("accounting continuity owner unavailable")
        account = ledger_record(owners[0])
        rows = [ledger_record(row) for row in db.execute("SELECT * FROM inference_cost_reservations")]
        if witness(account) != expected or ledger_digest(account, rows) != account["ledger_digest"]:
            raise ProductionWorkspaceReconciliationError("accounting continuity latest ledger unavailable")
        return account, rows


def acknowledge_accounting_period(*, root, period, expected_revision, actor):
    from src.workspace.accounting_witness import maintenance_accounting_lock, utc_period, period_state, ledger_digest, witness
    from src.workspace.production import ProductionWorkspaceReconciliationError, read_lifecycle_receipt, write_lifecycle_receipt, write_accounting_checkpoint
    if period != utc_period() or type(expected_revision) is not int:
        raise ProductionWorkspaceReconciliationError("accounting period review binding invalid")
    with maintenance_accounting_lock(root) as workspace:
        receipt = read_lifecycle_receipt(workspace) or {}
        account, rows = verify_accounting_generation(root / "seraph.db", receipt.get("inference_accounting"))
        if account["revision"] != expected_revision:
            raise ProductionWorkspaceReconciliationError("accounting period revision changed")
        state = period_state(account, rows, period)
        history = json.loads(account["settings_history_json"])
        history.append({"kind": "period_observed", "period_id": max(period, state["period_high_water"]), "recorded_at": datetime.now(timezone.utc).isoformat()})
        history.append({"kind": "period_review", "period_id": period, "accounting_revision": expected_revision,
            "actor": actor, "recorded_at": datetime.now(timezone.utc).isoformat(), "memory_status": "no_learning"})
        base = witness(account)
        account["settings_history_json"] = json.dumps(history, sort_keys=True, separators=(",", ":"))
        account["revision"] += 1
        account["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        account["ledger_digest"] = ledger_digest(account, rows)
        with sqlite3.connect(root / "seraph.db") as db:
            db.execute("BEGIN IMMEDIATE")
            names = ",".join('"'+name+'"=?' for name in account)
            update = db.execute(f'UPDATE inference_accounting_owners SET {names} WHERE deployment_id=? AND revision=?',
                (*account.values(), account["deployment_id"], expected_revision))
            if update.rowcount != 1:
                raise ProductionWorkspaceReconciliationError("accounting period revision changed")
            write_accounting_checkpoint(workspace, {"schema_version": 1, "base": base, "account": account,
                "operations": [], "witness": witness(account), "secret_values_included": False})
            receipt["inference_accounting"] = witness(account)
            write_lifecycle_receipt(workspace, receipt, _accounting_lock_held=True)
        return {"status": "acknowledged", "period_id": period, "revision": account["revision"],
            "period_high_water": max(period, state["period_high_water"]), "job_authority_changed": False, "memory_status": "no_learning"}


def rebind_accounting_root(*, active, target):
    from src.workspace.lifecycle import _require_production_fence
    from src.workspace import canonical_workspace_registry
    from src.workspace.production import read_lifecycle_receipt, write_lifecycle_receipt, ProductionWorkspaceReconciliationError
    from src.workspace.accounting_witness import maintenance_accounting_lock, revoke_restored_policy, _write_configuration_file
    _require_production_fence(canonical_workspace_registry(active.host_root))
    _require_production_fence(canonical_workspace_registry(target.host_root))
    receipt = read_lifecycle_receipt(active) or {}
    binding = receipt.get("deployment_binding", {})
    if binding.get("root_path_digest") == target.identity_digest:
        verify_accounting_generation(target.host_root / "seraph.db", receipt.get("inference_accounting"))
        return {"status": "already_rebound", "deployment_id": receipt["inference_accounting"]["deployment_id"], "job_authority_changed": False}
    if binding.get("root_path_digest") != active.identity_digest or active.lifecycle_directory != target.lifecycle_directory:
        raise ProductionWorkspaceReconciliationError("accounting rebind source descriptor unavailable")
    retained = retain_inference_accounting(active=active.host_root, target=target.host_root, database_path="seraph.db")
    if retained["status"] != "retained_latest":
        raise ProductionWorkspaceReconciliationError("accounting rebind deployment ledger unavailable")
    target_config = target.host_root / "model-fabric-settings.json"
    if not target_config.exists():
        source = active.host_root / "model-fabric-settings.json"
        if source.is_file() and not source.is_symlink():
            _write_configuration_file(target_config, json.loads(source.read_text()))
    revoke_restored_policy(active=active, target=target.host_root)
    with maintenance_accounting_lock(active) as workspace:
        receipt = read_lifecycle_receipt(workspace)
        receipt["deployment_binding"] = {"revision": receipt["deployment_binding"]["revision"] + 1,
            "root_path_digest": target.identity_digest, "host_bind_identity": target.bind_identity_digest}
        write_lifecycle_receipt(workspace, receipt, _accounting_lock_held=True)
    return {"status": "rebound", "deployment_id": receipt["inference_accounting"]["deployment_id"],
        "liabilities_retained": retained["operations"], "job_authority_changed": False, "memory_status": "no_learning"}


def reconcile_accounting_checkpoint(*, root: Path, registry) -> dict[str, object]:
    """Explicit maintenance repair of one witnessed, interrupted DB commit.

    The retained delta contains no callback/source content and cannot grant
    execution authority. Older arbitrary DB generations still require the
    latest retained ledger; this bounded checkpoint covers one commit gap.
    """
    from src.workspace.lifecycle import _require_production_fence
    from src.workspace.production import ProductionWorkspaceReconciliationError, read_lifecycle_receipt, read_accounting_checkpoint
    from src.workspace.accounting_witness import maintenance_accounting_lock as _continuity_lock, ledger_digest as _ledger_digest, witness as _witness, ledger_record
    _require_production_fence(registry)
    with _continuity_lock(root.resolve()) as workspace:
        receipt = read_lifecycle_receipt(workspace) or {}
        checkpoint = read_accounting_checkpoint(workspace)
        if checkpoint is None or checkpoint.get("witness") != receipt.get("inference_accounting") or checkpoint.get("schema_version") != 1:
            raise ProductionWorkspaceReconciliationError("accounting checkpoint authority unavailable")
        database = root / registry.config.database_path
        if not database.is_file() or database.is_symlink():
            raise ProductionWorkspaceReconciliationError("accounting checkpoint database unavailable")
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            owner_rows = connection.execute("SELECT * FROM inference_accounting_owners").fetchall()
            if len(owner_rows) > 1:
                raise ProductionWorkspaceReconciliationError("accounting checkpoint owner ambiguous")
            old_owner = ledger_record(owner_rows[0]) if owner_rows else None
            old_rows = [ledger_record(row) for row in connection.execute("SELECT * FROM inference_cost_reservations")]
            if old_owner is not None and _witness(old_owner) == checkpoint["witness"] and _ledger_digest(old_owner, old_rows) == old_owner["ledger_digest"]:
                return {"status": "already_reconciled", "revision": old_owner["revision"], "execution_authority_changed": False}
            if (checkpoint.get("base") != (_witness(old_owner) if old_owner is not None else None)
                or (old_owner is not None and _ledger_digest(old_owner, old_rows) != old_owner["ledger_digest"])):
                raise ProductionWorkspaceReconciliationError("accounting checkpoint base revision unavailable")
            account = ledger_record(checkpoint["account"])
            changes = checkpoint.get("operations")
            if not isinstance(changes, list) or len(changes) > 128 or account["revision"] != (old_owner["revision"] + 1 if old_owner is not None else 1):
                raise ProductionWorkspaceReconciliationError("accounting checkpoint revision invalid")
            rows_by_id = {row["operation_id"]: row for row in old_rows}
            for item in changes:
                row = ledger_record(item)
                prior = rows_by_id.get(row["operation_id"])
                if row["deployment_id"] != account["deployment_id"] or (prior is not None and row["revision"] < prior["revision"]):
                    raise ProductionWorkspaceReconciliationError("accounting checkpoint liability regression")
                rows_by_id[row["operation_id"]] = row
            if _witness(account) != checkpoint["witness"] or _ledger_digest(account, list(rows_by_id.values())) != account["ledger_digest"]:
                raise ProductionWorkspaceReconciliationError("accounting checkpoint digest mismatch")
            for table, values in (("inference_accounting_owners", [account]), ("inference_cost_reservations", [rows_by_id[item["operation_id"]] for item in changes])):
                for value in values:
                    fields = value
                    allowed = {entry[1] for entry in connection.execute(f'PRAGMA table_info("{table}")')}
                    if set(fields) != allowed:
                        raise ProductionWorkspaceReconciliationError("accounting checkpoint columns invalid")
                    names = ",".join('"' + name + '"' for name in fields)
                    connection.execute(f'INSERT OR REPLACE INTO "{table}" ({names}) VALUES ({",".join("?" for _ in fields)})', tuple(fields.values()))
            connection.commit()
            return {"status": "reconciled", "revision": account["revision"], "execution_authority_changed": False,
                "liabilities_retained": len(changes), "memory_status": "no_learning"}
        finally:
            connection.close()


def retain_inference_accounting(*, active: Path, target: Path, database_path: str) -> dict[str, object]:
    # Only the existing maintenance-fenced lifecycle calls this helper. Its
    # external witness also fences every live ledger mutation.
    from src.workspace.accounting_witness import maintenance_accounting_lock as _continuity_lock, ledger_digest as _ledger_digest, witness as _witness, ledger_record
    from src.workspace.production import ProductionWorkspace, ProductionWorkspaceReconciliationError, read_lifecycle_receipt

    source_path, target_path = active / database_path, target / database_path
    if not source_path.is_file() or source_path.is_symlink() or not target_path.is_file() or target_path.is_symlink():
        raise ProductionWorkspaceReconciliationError("accounting continuity databases unavailable")
    with sqlite3.connect(source_path) as inspection:
        exists = inspection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inference_accounting_owners'").fetchone()
        empty = not exists or not inspection.execute("SELECT 1 FROM inference_accounting_owners").fetchone()
        if empty:
            prior = read_lifecycle_receipt(ProductionWorkspace(host_root=active.resolve()))
            if prior and prior.get("inference_accounting"):
                raise ProductionWorkspaceReconciliationError("accounting continuity ledger missing")
            target_has = sqlite3.connect(target_path)
            try:
                target_exists = target_has.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inference_accounting_owners'").fetchone()
                if target_exists and target_has.execute("SELECT 1 FROM inference_accounting_owners").fetchone():
                    raise ProductionWorkspaceReconciliationError("accounting continuity restore authority unavailable")
            finally:
                target_has.close()
            return {"status": "not_initialized"}
    with _continuity_lock(active.resolve()) as workspace:
        source = sqlite3.connect(source_path)
        destination = sqlite3.connect(target_path)
        source.row_factory = sqlite3.Row
        destination.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            receipt = read_lifecycle_receipt(workspace) or {}
            if "inference_accounting_owners" not in tables:
                if receipt.get("inference_accounting"):
                    raise ProductionWorkspaceReconciliationError("accounting continuity ledger missing")
                return {"status": "not_initialized"}
            owner_rows = source.execute("SELECT * FROM inference_accounting_owners").fetchall()
            if not owner_rows:
                if receipt.get("inference_accounting"):
                    raise ProductionWorkspaceReconciliationError("accounting continuity owner missing")
                return {"status": "not_initialized"}
            if len(owner_rows) != 1:
                raise ProductionWorkspaceReconciliationError("accounting continuity owner ambiguous")
            account = ledger_record(owner_rows[0])
            operations = source.execute("SELECT * FROM inference_cost_reservations").fetchall()
            rows = [ledger_record(row) for row in operations]
            if receipt.get("inference_accounting") != _witness(account) or _ledger_digest(account, rows) != account["ledger_digest"]:
                raise ProductionWorkspaceReconciliationError("accounting continuity latest ledger unavailable")
            destination.execute("BEGIN IMMEDIATE")
            target_tables = {row[0] for row in destination.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("inference_accounting_owners", "inference_cost_reservations"):
                if table not in target_tables:
                    ddl = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
                    destination.execute(ddl)
            prior = destination.execute("SELECT * FROM inference_accounting_owners").fetchall()
            if prior and (len(prior) != 1 or prior[0]["deployment_id"] != account["deployment_id"] or prior[0]["revision"] > account["revision"]):
                raise ProductionWorkspaceReconciliationError("accounting continuity foreign or newer restore ledger")
            source_ids = {row["operation_id"] for row in rows}
            for row in destination.execute("SELECT operation_id, revision FROM inference_cost_reservations"):
                current = next((item for item in rows if item["operation_id"] == row["operation_id"]), None)
                if row["operation_id"] not in source_ids or current["revision"] < row["revision"]:
                    raise ProductionWorkspaceReconciliationError("accounting continuity unretained restore liability")
            jobs = []
            if rows:
                if "workflow_run_states" not in tables or "workflow_run_states" not in target_tables:
                    raise ProductionWorkspaceReconciliationError("accounting canonical job schema unavailable")
                for job_id in sorted({row["job_id"] for row in rows}):
                    source_job = source.execute("SELECT * FROM workflow_run_states WHERE run_identity=?", (job_id,)).fetchone()
                    if source_job is None:
                        raise ProductionWorkspaceReconciliationError("accounting canonical job missing")
                    target_job = destination.execute("SELECT * FROM workflow_run_states WHERE run_identity=?", (job_id,)).fetchone()
                    jobs.append((target_job is not None, _retained_job_values(source_job, target_job, account)))
            # This is the verified latest retained ledger, including complete
            # history and immutable operation ownership. Restored data cannot
            # overwrite it. Every linked job is refreshed from that source,
            # blocked and fenced, including rows already present in the target.
            for table, values in (("inference_accounting_owners", owner_rows), ("inference_cost_reservations", operations)):
                destination.execute(f'DELETE FROM "{table}"')
                if values:
                    columns = list(values[0].keys())
                    names = ",".join('"' + column + '"' for column in columns)
                    placeholders = ",".join("?" for _ in columns)
                    destination.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', [tuple(row) for row in values])
            for present, values in jobs:
                if present:
                    assignments = ",".join('"' + column + '"=?' for column in values if column != "id")
                    destination.execute(f'UPDATE workflow_run_states SET {assignments} WHERE id=?',
                        (*[value for column, value in values.items() if column != "id"], values["id"]))
                else:
                    names = ",".join('"' + column + '"' for column in values)
                    destination.execute(f'INSERT INTO workflow_run_states ({names}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))
            destination.commit()
            return {"status": "retained_latest", "revision": account["revision"], "operations": len(rows)}
        finally:
            source.close()
            destination.close()
