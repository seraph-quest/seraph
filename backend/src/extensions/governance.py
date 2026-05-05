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
