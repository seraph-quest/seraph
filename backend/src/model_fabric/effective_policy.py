"""Read-only deployment policy projection and its exact owning revoke action."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json

from fastapi import HTTPException

from .configuration import _configuration_payload, read_model_fabric_configuration, write_model_fabric_configuration


configuration_mutation_lock = asyncio.Lock()


def current_inference_policy() -> tuple[object, str]:
    configured = read_model_fabric_configuration()
    if configured.status != "ready" or configured.openrouter_setup is None or configured.egress_revoked:
        raise PermissionError("provider_policy_revoked_or_unavailable")
    payload = _configuration_payload(configured)
    # Credentials stay backend-only and do not grant egress authority.
    payload["openrouter_setup"].pop("credential_fingerprint", None)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return configured, digest


async def effective_policy_grants(operator) -> list[dict[str, object]]:
    configured = read_model_fabric_configuration()
    setup = configured.openrouter_setup
    if setup is None:
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
    return [{"kind": "provider_policy", "grant_id": "provider_policy:openrouter", "record_id": "openrouter",
        "boundary": "inference_egress", "purpose": "governed model inference",
        "source": "explicitly consented current-root capability inputs", "destination": "https://openrouter.ai/api/v1",
        "state": "blocked_configuration" if configured.status != "ready" else "revoked" if configured.egress_revoked else "active",
        "revision": configured.egress_revision, "expires_at": None,
        "origin": "deployment_policy", "limits": {"scope": "deployment", "request_timeout_seconds": setup.timeout_seconds,
            "spend_ceiling_microusd": setup.spend_ceiling_microusd, "accounting_status": accounting["status"]},
        "affected_jobs": jobs, "controls": ["revoke"], "authority_cache": False}]


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
    if grant_id != "provider_policy:openrouter" or type(revision) is not int or not 1 <= len(key) <= 256:
        raise HTTPException(status_code=422, detail="exact policy identity, revision and idempotency key required")
    async with configuration_mutation_lock:
        configured = read_model_fabric_configuration()
        if configured.openrouter_setup is None or configured.status != "ready":
            raise HTTPException(status_code=409, detail="provider policy unavailable")
        if configured.egress_revoked and configured.egress_revocation_key == key and configured.egress_revision == revision + 1:
            return {"status": "revoked", "grant_id": grant_id, "revision": configured.egress_revision, "credential_revoked": False}
        if configured.egress_revision != revision:
            raise HTTPException(status_code=409, detail="provider policy revision changed")
        write_model_fabric_configuration(replace(configured, egress_revoked=True,
            egress_revision=revision + 1, egress_revocation_key=key, updated_at=None))
    return {"status": "revoked", "grant_id": grant_id, "revision": revision + 1, "credential_revoked": False}
