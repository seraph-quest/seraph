"""Dependency-free encoding shared by the runtime and managed lifecycle CLI."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path

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


def ledger_record(values):
    record = dict(values)
    # Canonical writers own this unhashed private lookup projection. Raw
    # SQLite reads and archived checkpoints retain the existing financial wire.
    record.pop("group_lookup_key", None)
    for field in ("created_at", "updated_at", "deadline_at", "contact_started_at"):
        value = record.get(field)
        if isinstance(value, str):
            record[field] = datetime.fromisoformat(value).isoformat()
        elif isinstance(value, datetime):
            record[field] = value.isoformat()
    return record


def ledger_digest(account, rows):
    owner = ledger_record(account)
    owner.pop("ledger_digest", None)
    payload = {"account": owner, "operations": [ledger_record(row) for row in sorted(rows, key=lambda row: row["operation_id"])]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


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
