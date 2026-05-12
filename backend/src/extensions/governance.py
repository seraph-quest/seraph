"""Governance status evaluation for extension packages."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version

from src.extensions.manifest import ExtensionManifest, ExtensionTrust

SIGNATURE_ALGORITHM = "seraph-sha256-v1"
GOVERNED_MARKETPLACE_CLAIM_BOUNDARY = (
    "local_governed_marketplace_foundations_not_production_marketplace_security"
)
_TRUST_RANK = {
    ExtensionTrust.LOCAL.value: 1,
    ExtensionTrust.VERIFIED.value: 2,
    ExtensionTrust.BUNDLED.value: 3,
}


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


def _version_relation(candidate_version: str, current_version: str | None) -> str:
    if not current_version:
        return "new_install"
    try:
        candidate = Version(candidate_version)
        current = Version(current_version)
    except InvalidVersion:
        return "invalid_version"
    if candidate > current:
        return "upgrade"
    if candidate < current:
        return "downgrade"
    return "same_version"


def _compatibility_verdict(manifest: ExtensionManifest, seraph_version: str | None) -> dict[str, Any]:
    if not seraph_version:
        return {
            "status": "declared",
            "seraph": manifest.compatibility.seraph,
            "current_version": None,
            "compatible": None,
            "blocking_reason": None,
        }
    try:
        compatible = manifest.is_compatible_with(seraph_version)
    except ValueError as exc:
        return {
            "status": "invalid_runtime_version",
            "seraph": manifest.compatibility.seraph,
            "current_version": seraph_version,
            "compatible": False,
            "blocking_reason": str(exc),
        }
    return {
        "status": "compatible" if compatible else "incompatible_runtime",
        "seraph": manifest.compatibility.seraph,
        "current_version": seraph_version,
        "compatible": compatible,
        "blocking_reason": None if compatible else "candidate_seraph_compatibility_excludes_current_runtime",
    }


def _source_policy_status(manifest: ExtensionManifest) -> str:
    if manifest.trust == ExtensionTrust.BUNDLED:
        return "bundled_source_trusted"
    if manifest.governance is None:
        return "local_source_allowed"
    source = manifest.governance.provenance.source.strip().lower()
    if manifest.trust == ExtensionTrust.VERIFIED and source in {"local", "local-authoring", "workspace", "unknown"}:
        return "verified_source_policy_violation"
    if manifest.governance.source_policy:
        return "declared"
    return "defaulted"


def build_package_review_receipt(
    manifest: ExtensionManifest | None,
    *,
    root_path: str | Path | None,
    state_entry: dict[str, Any] | None = None,
    seraph_version: str | None = None,
    previous_manifest: ExtensionManifest | None = None,
) -> dict[str, Any]:
    current_digest = governance_package_digest(root_path)
    if manifest is None:
        return {
            "package_id": None,
            "status": "unknown",
            "trust": "unknown",
            "digest": current_digest,
            "claim_boundary": GOVERNED_MARKETPLACE_CLAIM_BOUNDARY,
        }

    state_governance = _state_governance(state_entry)
    permission_fingerprint = extension_permission_fingerprint(manifest)
    reviewed_permission_fingerprint = state_governance.get("reviewed_permission_fingerprint")
    compatibility = _compatibility_verdict(manifest, seraph_version)
    previous_trust = previous_manifest.trust.value if previous_manifest is not None else None
    trust_downgrade = (
        previous_trust is not None
        and _TRUST_RANK.get(manifest.trust.value, 0) < _TRUST_RANK.get(previous_trust, 0)
    )
    version_relation = _version_relation(
        manifest.version,
        previous_manifest.version if previous_manifest is not None else None,
    )
    signature = manifest.governance.signature if manifest.governance is not None else None
    provenance = (
        manifest.governance.provenance.model_dump(mode="json")
        if manifest.governance is not None
        else {"source": "local"}
    )
    manifest_review = (
        manifest.governance.review.model_dump(mode="json")
        if manifest.governance is not None and manifest.governance.review is not None
        else None
    )
    reviewed_digest = state_governance.get("reviewed_digest")
    reviewed_key_id = state_governance.get("reviewed_key_id")
    review_status = str(state_governance.get("review_status") or "not_required")
    if manifest.trust == ExtensionTrust.VERIFIED:
        if review_status in {"approved", "reviewed"} and reviewed_digest == current_digest:
            review_status = "reviewed"
        elif reviewed_digest:
            review_status = "stale"
        else:
            review_status = "missing"
        if permission_fingerprint != reviewed_permission_fingerprint and reviewed_permission_fingerprint:
            review_status = "permission_drift"

    supply_chain_status = "local_unsigned_allowed"
    if manifest.trust == ExtensionTrust.BUNDLED:
        supply_chain_status = "bundled_trusted"
    elif manifest.trust == ExtensionTrust.VERIFIED:
        if manifest.governance is None:
            supply_chain_status = "missing_governance"
        elif signature is None:
            supply_chain_status = "unsigned_verified"
        elif signature.algorithm != SIGNATURE_ALGORITHM:
            supply_chain_status = "unsupported_signature_algorithm"
        elif current_digest is not None and signature.digest != current_digest:
            supply_chain_status = "digest_mismatch"
        elif signature.signature != governance_signature_value(key_id=signature.key_id, digest=signature.digest):
            supply_chain_status = "invalid_signature"
        elif _source_policy_status(manifest) == "verified_source_policy_violation":
            supply_chain_status = "source_policy_violation"
        else:
            supply_chain_status = "verified_signature_valid"

    blocking_reasons: list[str] = []
    if compatibility["compatible"] is False:
        blocking_reasons.append(str(compatibility["blocking_reason"] or "incompatible_runtime"))
    if manifest.trust == ExtensionTrust.VERIFIED and review_status != "reviewed":
        blocking_reasons.append(f"review_{review_status}")
    if trust_downgrade:
        blocking_reasons.append("trust_downgrade")
    if version_relation == "downgrade" and (
        previous_manifest.trust == ExtensionTrust.VERIFIED
        or previous_manifest.kind.value == "connector-pack"
    ):
        blocking_reasons.append("version_downgrade")
    if manifest.trust == ExtensionTrust.VERIFIED and supply_chain_status != "verified_signature_valid":
        blocking_reasons.append(f"supply_chain_{supply_chain_status}")

    return {
        "package_id": manifest.id,
        "version": manifest.version,
        "trust": manifest.trust.value,
        "previous_trust": previous_trust,
        "version_relation": version_relation,
        "status": "blocked" if blocking_reasons else "reviewable",
        "digest": current_digest,
        "signed_digest": signature.digest if signature is not None else None,
        "key_id": signature.key_id if signature is not None else None,
        "reviewed_digest": reviewed_digest if isinstance(reviewed_digest, str) else None,
        "reviewed_key_id": reviewed_key_id if isinstance(reviewed_key_id, str) else None,
        "review_status": review_status,
        "manifest_review": manifest_review,
        "permission_fingerprint": permission_fingerprint,
        "reviewed_permission_fingerprint": (
            reviewed_permission_fingerprint if isinstance(reviewed_permission_fingerprint, str) else None
        ),
        "compatibility": compatibility,
        "compatibility_status": compatibility["status"],
        "supply_chain_status": supply_chain_status,
        "source_policy_status": _source_policy_status(manifest),
        "trust_downgrade_status": "blocked" if trust_downgrade else "not_downgraded",
        "blocking_reasons": blocking_reasons,
        "recommended_action": "reject_or_re_review" if blocking_reasons else "review_or_install",
        "claim_boundary": GOVERNED_MARKETPLACE_CLAIM_BOUNDARY,
    }


def build_governance_status(
    manifest: ExtensionManifest | None,
    *,
    root_path: str | Path | None,
    state_entry: dict[str, Any] | None = None,
    seraph_version: str | None = None,
) -> dict[str, Any]:
    current_digest = governance_package_digest(root_path)
    if manifest is None:
        review_receipt = build_package_review_receipt(None, root_path=root_path, state_entry=state_entry)
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
            "review_receipt": review_receipt,
            "claim_boundary": GOVERNED_MARKETPLACE_CLAIM_BOUNDARY,
        }

    review_receipt = build_package_review_receipt(
        manifest,
        root_path=root_path,
        state_entry=state_entry,
        seraph_version=seraph_version,
    )
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
            "review_receipt": review_receipt,
            "compatibility_status": review_receipt["compatibility_status"],
            "supply_chain_status": review_receipt["supply_chain_status"],
            "claim_boundary": GOVERNED_MARKETPLACE_CLAIM_BOUNDARY,
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
            "review_receipt": review_receipt,
            "compatibility_status": review_receipt["compatibility_status"],
            "supply_chain_status": review_receipt["supply_chain_status"],
            "claim_boundary": GOVERNED_MARKETPLACE_CLAIM_BOUNDARY,
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
    elif review_receipt["compatibility"].get("compatible") is False:
        fail_closed_reason = "compatibility_incompatible_runtime"
    elif review_receipt["supply_chain_status"] != "verified_signature_valid":
        fail_closed_reason = f"supply_chain_{review_receipt['supply_chain_status']}"

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
        "review_receipt": review_receipt,
        "compatibility_status": review_receipt["compatibility_status"],
        "supply_chain_status": review_receipt["supply_chain_status"],
        "source_policy_status": review_receipt["source_policy_status"],
        "claim_boundary": GOVERNED_MARKETPLACE_CLAIM_BOUNDARY,
    }


def assert_governance_allows_lifecycle(
    manifest: ExtensionManifest | None,
    *,
    root_path: str | Path | None,
    state_entry: dict[str, Any] | None,
    action: str,
    seraph_version: str | None = None,
) -> dict[str, Any]:
    status = build_governance_status(
        manifest,
        root_path=root_path,
        state_entry=state_entry,
        seraph_version=seraph_version,
    )
    if status.get("fail_closed"):
        reason = str(status.get("fail_closed_reason") or "governance blocked")
        raise ExtensionGovernanceError(
            f"extension governance blocks {action}: {reason}"
        )
    return status


def assert_governance_allows_update_transition(
    candidate_manifest: ExtensionManifest,
    *,
    existing_manifest: ExtensionManifest | None,
    root_path: str | Path | None,
    state_entry: dict[str, Any] | None,
    seraph_version: str | None = None,
) -> dict[str, Any]:
    receipt = build_package_review_receipt(
        candidate_manifest,
        root_path=root_path,
        state_entry=state_entry,
        seraph_version=seraph_version,
        previous_manifest=existing_manifest,
    )
    blocking_reasons = [str(reason) for reason in receipt.get("blocking_reasons", [])]
    if blocking_reasons:
        raise ExtensionGovernanceError(
            "extension governance blocks update transition: "
            + ",".join(blocking_reasons)
        )
    return receipt
