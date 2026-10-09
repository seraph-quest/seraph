"""Read-only deployment policy projection and its exact owning revoke action."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json

from fastapi import HTTPException

from .configuration import _configuration_payload, read_model_fabric_configuration, write_model_fabric_configuration


configuration_mutation_lock = asyncio.Lock()


def current_near_text_policy() -> tuple[object, str]:
    configured = read_model_fabric_configuration()
    setup = configured.near_text
    if configured.status != "ready" or configured.egress_revoked or setup is None or not setup.enabled:
        raise PermissionError("near_text_policy_revoked_or_unavailable")
    if setup.plaintext_egress_consent_revision != configured.egress_revision:
        raise PermissionError("near_text_plaintext_consent_required")
    from .configuration import deployment_spend_ceiling
    deployment_spend_ceiling(configured)
    payload = _configuration_payload(configured)
    payload["near_text"].pop("credential_fingerprint", None)
    if payload.get("openrouter_setup") is not None:
        payload["openrouter_setup"].pop("credential_fingerprint", None)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return configured, digest


def current_inference_policy() -> tuple[object, str]:
    return inference_policy_from_configuration(read_model_fabric_configuration())


def inference_policy_from_configuration(configured) -> tuple[object, str]:
    """Exact pure owner projection for an already validated configuration."""
    if configured.status != "ready" or configured.openrouter_setup is None or configured.egress_revoked or not configured.openrouter_setup.cloud_egress_acknowledged:
        raise PermissionError("provider_policy_revoked_or_unavailable")
    payload = _configuration_payload(configured)
    # Credentials stay backend-only and do not grant egress authority.
    payload["openrouter_setup"].pop("credential_fingerprint", None)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return configured, digest


async def effective_policy_grants(operator) -> list[dict[str, object]]:
    configured = read_model_fabric_configuration()
    setup = configured.openrouter_setup
    if setup is None and configured.near_text is None:
        return []
    from src.workflows.job_runtime import durable_job_repository

    accounting = await durable_job_repository.inference_accounting_snapshot()
    operations = accounting.get("operations", [])
    principal_id = getattr(getattr(operator, "principal", None), "principal_id", None)
    session_id = getattr(operator, "session_id", None)
    jobs = []
    owned = [row for row in operations if row.get("owner_id") == principal_id and row.get("state") in {"reserved", "contact_started", "unknown"}]
    for row in owned[:100]:
        job = await durable_job_repository.get_job(str(row["job_id"]))
        if job is None or (session_id is not None and job.get("session_id") != session_id):
            continue
        jobs.append({"job_id": row["job_id"], "state": job["status"], "kind": job.get("job_kind", "model_inference")})
    grants = []
    if setup is not None:
        grants.append({"kind": "provider_policy", "grant_id": "provider_policy:openrouter", "record_id": "openrouter",
        "boundary": "inference_egress", "purpose": "governed model inference",
        "source": "explicitly consented current-root capability inputs", "destination": "https://openrouter.ai/api/v1",
        "state": "blocked_configuration" if configured.status != "ready" else "revoked" if configured.egress_revoked or not setup.cloud_egress_acknowledged else "active",
        "revision": configured.egress_revision, "expires_at": None,
        "origin": "deployment_policy", "limits": {"scope": "deployment", "request_timeout_seconds": setup.timeout_seconds,
            "spend_ceiling_microusd": setup.spend_ceiling_microusd, "accounting_status": accounting["status"]},
        "affected_jobs": jobs, "controls": ["revoke"], "authority_cache": False})
    near = configured.near_text
    if near is not None:
        grants.append({"kind": "provider_policy", "grant_id": "provider_policy:near_text", "record_id": "near_text",
            "boundary": "inference_egress", "purpose": "optional text inference; provider receives plaintext",
            "source": "explicitly consented current-root capability inputs", "destination": near.api_base,
            "state": "blocked_configuration" if configured.status != "ready" else "revoked" if configured.egress_revoked else "disabled" if not near.enabled else "active" if near.plaintext_egress_consent_revision == configured.egress_revision else "blocked_consent",
            "revision": configured.egress_revision, "expires_at": None, "origin": "deployment_policy",
            "limits": {"scope": "deployment", "revoke_scope": "OpenRouter and NEAR",
                "request_timeout_seconds": near.timeout_seconds, "spend_ceiling_microusd": near.spend_ceiling_microusd,
                "accounting_status": accounting["status"]}, "affected_jobs": jobs,
            "controls": ["revoke"], "authority_cache": False})
    return grants


async def revoke_effective_policy(request, body):
    from src.approval.runtime import get_current_trust_principal
    from src.auth.service import authenticate_principal

    principal = getattr(getattr(request.state, "operator", None), "principal", None) or get_current_trust_principal()
    if principal is None or not principal.authenticated:
        raise HTTPException(status_code=403, detail="authenticated deployment policy owner required")
    await authenticate_principal(principal.principal_id)
    grant_id = getattr(body, "grant_id", None)
    revision = getattr(body, "expected_revision", None)
    key = str(getattr(body, "idempotency_key", "") or "")
    if grant_id not in {"provider_policy:openrouter", "provider_policy:near_text"} or type(revision) is not int or not 1 <= len(key) <= 256:
        raise HTTPException(status_code=422, detail="exact policy identity, revision and idempotency key required")
    async with configuration_mutation_lock:
        configured = read_model_fabric_configuration()
        available = configured.openrouter_setup is not None if grant_id == "provider_policy:openrouter" else configured.near_text is not None
        if not available or configured.status != "ready":
            raise HTTPException(status_code=409, detail="provider policy unavailable")
        if configured.egress_revoked and configured.egress_revocation_key == key and configured.egress_revision == revision + 1:
            return {"status": "revoked", "grant_id": grant_id, "revision": configured.egress_revision, "credential_revoked": False}
        if configured.egress_revision != revision:
            raise HTTPException(status_code=409, detail="provider policy revision changed")
        write_model_fabric_configuration(replace(configured, egress_revoked=True,
            egress_revision=revision + 1, egress_revocation_key=key, updated_at=None))
    return {"status": "revoked", "grant_id": grant_id, "revision": revision + 1, "credential_revoked": False}
