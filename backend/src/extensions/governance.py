"""Governance status evaluation for extension packages."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from src.extensions.manifest import ExtensionManifest, ExtensionTrust

SIGNATURE_ALGORITHM = "seraph-sha256-v1"


class ExtensionGovernanceError(ValueError):
    """Raised when extension governance blocks a lifecycle action."""


def extension_permission_fingerprint(manifest: ExtensionManifest) -> str:
    payload = manifest.permissions.model_dump(mode="json")
    encoded = yaml.safe_dump(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def governance_signature_value(*, key_id: str, digest: str) -> str:
    return f"{SIGNATURE_ALGORITHM}:{key_id}:{digest}"


def _manifest_path(root: Path) -> Path | None:
    for manifest_name in ("manifest.yaml", "manifest.yml"):
        candidate = root / manifest_name
        if candidate.is_file():
            return candidate
    return None


def _manifest_bytes_for_governance(manifest_path: Path) -> bytes:
    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return manifest_path.read_bytes()
    if not isinstance(payload, dict):
        return manifest_path.read_bytes()
    governance = payload.get("governance")
    if isinstance(governance, dict):
        governance = dict(governance)
        governance.pop("signature", None)
        payload = dict(payload)
        payload["governance"] = governance
    return yaml.safe_dump(payload, sort_keys=True).encode("utf-8")


def governance_package_digest(root_path: str | Path | None) -> str | None:
    if root_path is None:
        return None
    root = Path(root_path)
    if not root.exists() or not root.is_dir():
        return None
    manifest_path = _manifest_path(root)
    hasher = hashlib.sha256()
    for file_path in sorted(path for path in root.rglob("*") if path.is_file()):
        relative_path = file_path.relative_to(root).as_posix()
        hasher.update(relative_path.encode("utf-8"))
        hasher.update(b"\0")
        if manifest_path is not None and file_path == manifest_path:
            hasher.update(_manifest_bytes_for_governance(file_path))
            continue
        with file_path.open("rb") as handle:
            while True:
                chunk = handle.read(8192)
                if not chunk:
                    break
                hasher.update(chunk)
    return hasher.hexdigest()


def _state_governance(state_entry: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state_entry, dict):
        return {}
    governance = state_entry.get("governance")
    return governance if isinstance(governance, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def build_governance_status(
    manifest: ExtensionManifest | None,
    *,
    root_path: str | Path | None,
    state_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_digest = governance_package_digest(root_path)
    if manifest is None:
        return {
            "status": "unknown",
            "trust": "unknown",
            "signature_status": "unknown",
            "review_status": "unknown",
            "revocation_status": "unknown",
            "provenance": None,
            "current_digest": current_digest,
            "signed_digest": None,
            "reviewed_digest": None,
            "signing_key_id": None,
            "reviewed_key_id": None,
            "permission_drift": False,
            "fail_closed": False,
            "fail_closed_reason": None,
        }

    state_governance = _state_governance(state_entry)
    permission_fingerprint = extension_permission_fingerprint(manifest)
    reviewed_permission_fingerprint = state_governance.get("reviewed_permission_fingerprint")
    permission_drift = (
        isinstance(reviewed_permission_fingerprint, str)
        and reviewed_permission_fingerprint
        and reviewed_permission_fingerprint != permission_fingerprint
    )
    reviewed_digest = state_governance.get("reviewed_digest")
    reviewed_key_id = state_governance.get("reviewed_key_id")
    review_state = str(state_governance.get("review_status") or "approved")

    if manifest.trust == ExtensionTrust.BUNDLED:
        return {
            "status": "bundled_trusted",
            "trust": manifest.trust.value,
            "signature_status": "bundled",
            "review_status": "not_required",
            "revocation_status": "not_revoked",
            "provenance": {"source": "bundled"},
            "current_digest": current_digest,
            "signed_digest": None,
            "reviewed_digest": None,
            "signing_key_id": None,
            "reviewed_key_id": None,
            "permission_fingerprint": permission_fingerprint,
            "reviewed_permission_fingerprint": None,
            "permission_drift": False,
            "fail_closed": False,
            "fail_closed_reason": None,
        }

    if manifest.trust == ExtensionTrust.LOCAL:
        provenance = (
            manifest.governance.provenance.model_dump(mode="json")
            if manifest.governance is not None
            else {"source": "local"}
        )
        return {
            "status": "local_unsigned",
            "trust": manifest.trust.value,
            "signature_status": "unsigned_allowed",
            "review_status": "not_required",
            "revocation_status": "not_revoked",
            "provenance": provenance,
            "current_digest": current_digest,
            "signed_digest": None,
            "reviewed_digest": None,
            "signing_key_id": None,
            "reviewed_key_id": None,
            "permission_fingerprint": permission_fingerprint,
            "reviewed_permission_fingerprint": None,
            "permission_drift": False,
            "fail_closed": False,
            "fail_closed_reason": None,
        }

    signature = manifest.governance.signature if manifest.governance is not None else None
    provenance = (
        manifest.governance.provenance.model_dump(mode="json")
        if manifest.governance is not None
        else None
    )
    signed_digest = signature.digest if signature is not None else None
    signing_key_id = signature.key_id if signature is not None else None

    signature_status = "missing"
    if signature is not None and current_digest is not None:
        expected_signature = governance_signature_value(key_id=signature.key_id, digest=signature.digest)
        if signature.algorithm != SIGNATURE_ALGORITHM:
            signature_status = "unsupported_algorithm"
        elif signature.digest != current_digest:
            signature_status = "digest_mismatch"
        elif signature.signature != expected_signature:
            signature_status = "invalid"
        else:
            signature_status = "valid"

    revoked_digest_values = set(_string_list(state_governance.get("revoked_digests")))
    revoked_key_values = set(_string_list(state_governance.get("revoked_key_ids")))
    revoked = (
        bool(state_governance.get("revoked"))
        or (current_digest is not None and current_digest in revoked_digest_values)
        or (isinstance(signing_key_id, str) and signing_key_id in revoked_key_values)
    )
    revocation_status = "revoked" if revoked else "not_revoked"
    revocation_reason = (
        str(state_governance.get("revocation_reason"))
        if state_governance.get("revocation_reason") is not None
        else None
    )

    if review_state not in {"approved", "reviewed"}:
        review_status = review_state
    elif not isinstance(reviewed_digest, str) or not reviewed_digest:
        review_status = "missing"
    elif reviewed_digest != current_digest:
        review_status = "stale"
    elif isinstance(reviewed_key_id, str) and signing_key_id and reviewed_key_id != signing_key_id:
        review_status = "stale_key"
    elif permission_drift:
        review_status = "permission_drift"
    else:
        review_status = "reviewed"

    fail_closed_reason = None
    if signature_status != "valid":
        fail_closed_reason = f"signature_{signature_status}"
    elif revocation_status == "revoked":
        fail_closed_reason = revocation_reason or "revoked"
    elif review_status != "reviewed":
        fail_closed_reason = f"review_{review_status}"

    return {
        "status": "verified" if fail_closed_reason is None else "blocked",
        "trust": manifest.trust.value,
        "signature_status": signature_status,
        "review_status": review_status,
        "revocation_status": revocation_status,
        "provenance": provenance,
        "current_digest": current_digest,
        "signed_digest": signed_digest,
        "reviewed_digest": reviewed_digest if isinstance(reviewed_digest, str) else None,
        "signing_key_id": signing_key_id,
        "reviewed_key_id": reviewed_key_id if isinstance(reviewed_key_id, str) else None,
        "permission_fingerprint": permission_fingerprint,
        "reviewed_permission_fingerprint": (
            reviewed_permission_fingerprint
            if isinstance(reviewed_permission_fingerprint, str)
            else None
        ),
        "permission_drift": permission_drift,
        "fail_closed": fail_closed_reason is not None,
        "fail_closed_reason": fail_closed_reason,
    }


def assert_governance_allows_lifecycle(
    manifest: ExtensionManifest | None,
    *,
    root_path: str | Path | None,
    state_entry: dict[str, Any] | None,
    action: str,
) -> dict[str, Any]:
    status = build_governance_status(manifest, root_path=root_path, state_entry=state_entry)
    if status.get("fail_closed"):
        reason = str(status.get("fail_closed_reason") or "governance blocked")
        raise ExtensionGovernanceError(
            f"extension governance blocks {action}: {reason}"
        )
    return status


def build_capability_pack_hardening_receipt(
    manifest: ExtensionManifest | None,
    *,
    governance_status: dict[str, Any],
    compatibility: dict[str, Any] | None = None,
    lifecycle_plan: dict[str, Any] | None = None,
    diagnostics_summary: dict[str, Any] | None = None,
    permission_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    compatibility = compatibility if isinstance(compatibility, dict) else {}
    lifecycle_plan = lifecycle_plan if isinstance(lifecycle_plan, dict) else {}
    diagnostics_summary = diagnostics_summary if isinstance(diagnostics_summary, dict) else {}
    permission_summary = permission_summary if isinstance(permission_summary, dict) else {}
    current_version = lifecycle_plan.get("current_version")
    candidate_version = lifecycle_plan.get("candidate_version") or (manifest.version if manifest is not None else None)
    version_relation = str(lifecycle_plan.get("version_relation") or "unknown")
    permission_missing = permission_summary.get("missing") if isinstance(permission_summary.get("missing"), dict) else {}
    has_missing_permissions = any(bool(value) for value in permission_missing.values())
    compatible = compatibility.get("compatible")
    incompatible = compatible is False
    fail_closed = bool(governance_status.get("fail_closed"))
    review_status = str(governance_status.get("review_status") or "unknown")
    signature_status = str(governance_status.get("signature_status") or "unknown")
    trust = str(governance_status.get("trust") or (manifest.trust.value if manifest is not None else "unknown"))
    degraded_count = int(diagnostics_summary.get("degraded_connector_count") or 0)
    error_count = int(diagnostics_summary.get("error_issue_count") or 0)
    package_changed = bool(lifecycle_plan.get("package_changed"))
    update_supported = bool(lifecycle_plan.get("update_supported"))

    risk_deltas: list[str] = []
    blocked_claims: list[str] = ["package_count_superiority"]
    negative_cases: list[str] = []
    if incompatible:
        risk_deltas.append("compatibility_block")
        blocked_claims.append("runtime_compatible_pack")
        negative_cases.append("incompatible_pack_version")
    if has_missing_permissions:
        risk_deltas.append("permission_expansion_or_underdeclaration")
        blocked_claims.append("complete_permission_declaration")
        negative_cases.append("underdeclared_permissions")
    if version_relation == "downgrade":
        risk_deltas.append("provider_or_pack_downgrade")
        negative_cases.append("provider_downgrade")
    if signature_status not in {"valid", "bundled", "unsigned_allowed"}:
        risk_deltas.append("supply_chain_integrity_change")
        blocked_claims.append("trusted_supply_chain")
        negative_cases.append("supply_chain_suspicion")
    if fail_closed:
        risk_deltas.append("runtime_access_removed")
        negative_cases.append("failed_update")
    if review_status in {"permission_drift", "stale"} or bool(governance_status.get("permission_drift")):
        risk_deltas.append("permission_review_drift")
        blocked_claims.append("reviewed_permission_envelope")
        negative_cases.append("extension_permission_creep")
    if package_changed or update_supported:
        negative_cases.append("rollback_need")
    if degraded_count:
        risk_deltas.append("provider_runtime_degraded")
        negative_cases.append("provider_downgrade")
    if error_count:
        risk_deltas.append("package_validation_errors")

    risk_deltas = sorted(set(risk_deltas)) or ["no_material_risk_delta_detected"]
    negative_cases = sorted(set(negative_cases))
    rollback_available = (
        bool(current_version)
        and bool(candidate_version)
        and version_relation in {"upgrade", "downgrade"}
        and bool(lifecycle_plan.get("current_location"))
    )
    rollback_action = "restore_previous_workspace_pack" if rollback_available else "not_available"
    if version_relation == "new":
        rollback_action = "remove_new_pack"
        rollback_available = True

    operator_summary = (
        "Pack transition is blocked until governance review, compatibility, permissions, and diagnostics are resolved."
        if fail_closed or incompatible or has_missing_permissions or error_count
        else "Pack transition is reviewable with explicit compatibility, permission, risk, and rollback posture."
    )
    return {
        "receipt_id": f"capability_pack_hardening:{manifest.id if manifest is not None else 'unknown'}",
        "extension_id": manifest.id if manifest is not None else None,
        "trust": trust,
        "current_version": current_version,
        "candidate_version": candidate_version,
        "version_relation": version_relation,
        "compatibility": compatibility or None,
        "review_status": review_status,
        "signature_status": signature_status,
        "fail_closed": fail_closed,
        "risk_deltas": risk_deltas,
        "negative_cases": negative_cases,
        "rollback": {
            "available": rollback_available,
            "action": rollback_action,
            "requires_review": trust == ExtensionTrust.VERIFIED.value or fail_closed,
        },
        "operator_summary": operator_summary,
        "blocked_claims": sorted(set(blocked_claims)),
        "claim_boundary": (
            "governed_capability_pack_hardening_receipts_not_production_marketplace_security_or_ecosystem_maturity_or_package_count_superiority"
        ),
    }
