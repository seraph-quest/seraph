"""Private contracts for the two accepted finite GitHub capacity-close routes.

Requests contain locators and exact CAS identities only. Positive effect proof
is minted from the existing protected adapter and trusted fixed producer.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.extensions.github_consent import digest


class _CloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    acknowledged_capacity_close: bool
    expected_job_revision: int = Field(gt=0)
    expected_connection_revision: int = Field(gt=0)
    expected_connection_fence: int = Field(gt=0)
    idempotency_key: str

    @field_validator("acknowledged_capacity_close", mode="before")
    @classmethod
    def explicit_close(cls, value):
        if value is not True:
            raise ValueError("explicit capacity-close acknowledgment required")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def exact_uuid(cls, value):
        if str(uuid.UUID(value)) != value:
            raise ValueError("canonical lower-case UUID required")
        return value


class PublicationCloseRequest(_CloseRequest):
    remote_commit_id: str | None = None
    pr_number: int | None = Field(default=None, gt=0)

    @field_validator("remote_commit_id")
    @classmethod
    def exact_git_id(cls, value):
        if value is not None and re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("exact remote Git SHA required")
        return value


class LegacyCloseRequest(_CloseRequest):
    remote_id: int | None = Field(default=None, gt=0)


class ReadWindow:
    """One finite whole-operation GET window, never a new execution deadline."""
    def __init__(self, *, legacy=False):
        self.deadline = time.monotonic() + 120
        self.max_calls = 4 if legacy else 4096
        self.max_raw = 4 * 1024 * 1024 if legacy else 192 * 1024 * 1024
        self.max_decoded = 4 * 1024 * 1024 if legacy else 128 * 1024 * 1024
        self.calls = self.raw_bytes = self.decoded_bytes = 0

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("github_capacity_read_window_expired")
        return min(20.0, remaining)

    def before_get(self):
        self.remaining()
        if self.calls >= self.max_calls:
            raise ValueError("github_capacity_read_call_limit")
        self.calls += 1

    def after_get(self, content):
        self.remaining()
        self.raw_bytes += len(content)
        if self.raw_bytes > self.max_raw:
            raise ValueError("github_capacity_read_raw_limit")

    def decoded(self, size):
        self.remaining()
        self.decoded_bytes += size
        if self.decoded_bytes > self.max_decoded:
            raise ValueError("github_capacity_read_decoded_limit")

    def projection(self):
        self.remaining()
        return {"calls": self.calls, "raw_bytes": self.raw_bytes,
            "decoded_bytes": self.decoded_bytes, "limit_seconds": 120,
            "limit_calls": self.max_calls, "limit_raw_bytes": self.max_raw,
            "limit_decoded_bytes": self.max_decoded}


def effect_identity(current, effect):
    return {"job_id": current["job_id"], "attempt_count": current["attempt_count"],
        "authority_digest": current["authority_digest"],
        **{key: effect.get(key) for key in ("effect_id", "effect_type", "target_path",
            "target_digest", "adapter_idempotency_key")}}


_OBSERVATION_INVENTORY_ORIGIN = object()


@dataclass(frozen=True)
class _ObservationInventory:
    job_id: str
    revision: int
    ledger_digest: str
    observations: tuple[str, ...]
    _origin: object


def _safe_approval_resume(current, effect):
    expected = {"kind": "approval_resume", "status": "approved",
        "authority_digest": current["authority_digest"],
        "approval_id": current["declared_authority"].get("approval_id"),
        "operator_principal_id": current["owner"]["principal_id"],
        "operator_session_id": current["operator_session_id"],
        "owner_kind": "user", "owner_principal_id": current["owner"]["principal_id"],
        "service_id": None, "goal_id": current.get("goal_id"),
        "goal_revision": current.get("goal_revision"), "plan_revision": current.get("plan_revision"),
        "capability_version": current.get("capability_version"), "budget_digest": current.get("budget_digest")}
    allowed = set(expected) | {"budget_microusd", "expires_at", "request_idempotency_key", "recorded_at",
        "approval_request_fingerprint", "approval_request_status"}
    return (expected["approval_id"] is not None and set(effect) <= allowed
        and all(effect.get(key) == value for key, value in expected.items())
        and type(effect.get("budget_microusd")) is int and effect["budget_microusd"] >= 0
        and type(effect.get("expires_at")) in {int, float} and math.isfinite(effect["expires_at"])
        and isinstance(effect.get("recorded_at"), str)
        and ("approval_request_status" not in effect or effect["approval_request_status"] == "consumed")
        and ("approval_request_fingerprint" not in effect or re.fullmatch(r"[0-9a-f]{64}", effect["approval_request_fingerprint"] or "") is not None))


def original_effects(current):
    """Exact complete canonical inventory; diagnostic observations add no intent."""
    effects = current.get("effects")
    if not isinstance(effects, list) or not 1 <= len(effects) <= 4096:
        raise ValueError("github_capacity_effect_inventory_missing")
    ids = set()
    original = []
    for effect in effects:
        if isinstance(effect, dict) and effect.get("kind") == "approval_resume":
            if _safe_approval_resume(current, effect):
                continue
            raise ValueError("github_capacity_approval_receipt_binding_invalid")
        if not isinstance(effect, dict) or not isinstance(effect.get("effect_id"), str) or effect["effect_id"] in ids:
            raise ValueError("github_capacity_effect_inventory_invalid")
        ids.add(effect["effect_id"])
        kind = effect.get("effect_type")
        if current["job_kind"] not in {"github_followthrough_v1", "engineering.repo-publication.v1"}:
            raise ValueError("github_capacity_native_kind_invalid")
        allowed = kind == "github_publication" if current["job_kind"] == "github_followthrough_v1" else kind in {"repo_publication_local_producer", "repo_publication_tree", "repo_publication_commit", "repo_publication_branch", "repo_publication_pr"} or isinstance(kind, str) and re.fullmatch(r"repo_publication_blob_[0-9a-f]{16}", kind) is not None
        # Even settled/failed unknown writes may have contacted a provider.
        if not allowed:
            raise ValueError("github_capacity_unproved_other_intent")
        details = effect.get("details") or {}
        if not isinstance(details, dict):
            raise ValueError("github_capacity_effect_inventory_invalid")
        diagnostic = details.get("observation_only") is True or details.get("readback_observation_only") is True or effect.get("original_effect_id") is not None or details.get("original_effect_id") is not None
        if diagnostic:
            staged = current.get("_closure_observation_inventory")
            if type(staged) is not _ObservationInventory or staged._origin is not _OBSERVATION_INVENTORY_ORIGIN or staged.job_id != current["job_id"] or staged.revision != current["revision"] or staged.ledger_digest != digest(effects) or digest(effect) not in staged.observations:
                raise ValueError("github_capacity_protected_observation_inventory_missing")
            continue
        original.append(effect)
    if not original or current["job_kind"] == "github_followthrough_v1" and len(original) != 1:
        raise ValueError("github_capacity_effect_inventory_missing")
    return original


async def stage_observation_inventory(current):
    """Bounded actual private bytes and canonical protected receipts PRE tx."""
    from sqlmodel import select
    from src.db import engine
    from src.db.models import WorkflowRunState
    from src.extensions.github_recovery import job_binding, KINDS
    from src.workflows.repo_publication import read_file
    effects = current["effects"]
    candidates = [item for item in effects if isinstance(item, dict) and (
        isinstance(item.get("details"), dict) and any(item["details"].get(key) is not None for key in ("observation_only", "readback_observation_only", "original_effect_id")) or item.get("original_effect_id") is not None)]
    if not candidates:
        return
    if len(candidates) > 4096:
        raise ValueError("github_capacity_observation_inventory_bounds")
    async with engine.get_session() as db:
        run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == current["job_id"]))).scalars().one()
        if run.revision != current["revision"] or digest(json.loads(run.effect_receipts_json)) != digest(effects):
            raise ValueError("github_capacity_observation_inventory_changed")
        canonical_job = job_binding(run)
        history = json.loads(run.github_read_observation_history_json or "[]")
        latest = json.loads(run.github_read_revision_json or "null")
        if latest is not None: history = history + [latest]
    if not isinstance(history, list) or len(history) > 4097:
        raise ValueError("github_capacity_observation_inventory_bounds")
    staged, aggregate = [], 0
    for item in candidates:
        details = item.get("details") or {}
        original = next((prior for prior in effects if prior.get("effect_id") == details.get("original_effect_id")), None)
        if original is None:
            raise ValueError("github_capacity_observation_original_missing")
        identity = effect_identity(current, original)
        receipt = next((receipt for receipt in history if isinstance(receipt, dict)
            and receipt.get("receipt_digest") == digest({key: value for key, value in receipt.items() if key != "receipt_digest"})
            and receipt.get("schema") == "seraph.github-read-revision-receipt.v1"
            and receipt.get("public_capability") == KINDS.get(current["job_kind"])
            and receipt.get("binding", {}).get("job") == canonical_job
            and receipt.get("binding", {}).get("original_write_binding") == current["declared_authority"].get("github_consent")
            and receipt.get("effect_identity") == identity
            and receipt.get("effect_readback", {}).get("readback_id") == item.get("readback_id")
            and receipt.get("effect_readback", {}).get("verified_at") == item.get("verified_at")
            and receipt.get("observation_id", item["effect_id"]) == item["effect_id"]
            and receipt.get("observation_artifact_sha256", item.get("content_sha256")) == item.get("content_sha256")
            and receipt.get("observation_artifact_id", details.get("artifact_id")) == details.get("artifact_id")), None)
        sha = item.get("content_sha256")
        if receipt is None or not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
            raise ValueError("github_capacity_protected_observation_inventory_missing")
        path = f"artifacts/github-observations/{current['job_id']}/{sha}.json"
        artifact = next((value for value in current["artifacts"] if value.get("artifact_id") == details.get("artifact_id") and value.get("artifact_type") == "github_recovery_observation" and value.get("file_path") == path and value.get("content_sha256") == sha and value.get("exists") is True), None)
        if artifact is None or item.get("receipt_kind") != "readback" or item.get("status") != "succeeded" or item.get("effect_id") != original["effect_id"] + ":observation:" + sha[:16] or any(item.get(key) != original.get(key) for key in ("effect_type", "target_path", "target_digest", "adapter_idempotency_key")):
            raise ValueError("github_capacity_observation_registration_changed")
        raw = read_file(path, maximum=256 * 1024)
        aggregate += len(raw)
        if aggregate > 16 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != sha or len(raw) != artifact.get("size_bytes"):
            raise ValueError("github_capacity_observation_bytes_changed")
        value = json.loads(raw)
        expected = {"schema": "seraph.github-effect-observation.v1", **identity,
            "readback_id": item["readback_id"], "verified_at": item["verified_at"]}
        if set(value) != set(expected) | {"remote_readback"} or any(value.get(key) != expected_value for key, expected_value in expected.items()) or value.get("remote_readback", {}).get("readback_path") != receipt.get("readback_path") or value.get("remote_readback", {}).get("payload_sha256") != receipt.get("raw_payload_sha256"):
            raise ValueError("github_capacity_observation_bytes_binding_changed")
        staged.append(digest(item))
    current["_closure_observation_inventory"] = _ObservationInventory(current["job_id"], current["revision"], digest(effects), tuple(staged), _OBSERVATION_INVENTORY_ORIGIN)


_CLOSURE_SEAL = object()


@dataclass(frozen=True)
class _CompleteClosureProof:
    original_job: dict
    binding: dict
    effect_inventory_sha256: str
    positive_gets: tuple
    producer: dict | None
    window: dict
    deadline: float
    _seal: object


def _mint_complete_proof(*, current, binding, positive_gets, guard_fd, window):
    # Only fixed service code calls this after exact adapter semantic seals
    # and trusted producer terminal verification. The repository repeats all
    # identities and completeness checks before the atomic CAS.
    original_effects(current)
    producer = None
    if current["job_kind"] == "engineering.repo-publication.v1":
        from src.execution.repo_publication_supervisor import terminal, canonical
        checkpoint = next((item.get("payload") for item in current.get("checkpoints", [])
            if item.get("checkpoint_id") == "publication_supervisor_admission"), None)
        if not isinstance(checkpoint, dict):
            raise ValueError("publication_supervisor_admission_missing")
        actual = terminal(checkpoint)
        from src.workflows.repo_publication import RepoPublicationService
        preview = RepoPublicationService().preview(current)
        local = [item for item in original_effects(current) if item["effect_type"] == "repo_publication_local_producer"]
        expected_local = {"profile": preview["local_posture"], "base_commit": preview["base_commit"],
            "patch_sha256": preview["repair_binding"]["patch_sha256"]}
        if len(local) != 1 or local[0].get("target_path") != f"artifacts/repo-publication/{current['job_id']}/producer" or local[0].get("target_digest") != digest(expected_local):
            raise ValueError("publication_local_producer_intent_binding_changed")
        metadata = os.fstat(guard_fd)
        if [metadata.st_dev, metadata.st_ino] != checkpoint.get("guard_identity"):
            raise ValueError("publication_producer_guard_changed")
        expected = {"job_id": current["job_id"], "root": current["operator_session_id"],
            "principal": current["owner"]["principal_id"], "attempt": current["attempt_count"],
            "authority_digest": current["authority_digest"], "input_digest": current["input_digest"],
            "run_fingerprint": current["run_fingerprint"], "goal_id": current["goal_id"],
            "goal_revision": current["goal_revision"],
            "preview_digest": current["declared_authority"]["preview_digest"]}
        if any(actual["binding"].get(key) != value for key, value in expected.items()) or actual["binding"].get("fence") != checkpoint["binding"].get("fence"):
            raise ValueError("publication_supervisor_canonical_binding_changed")
        producer = {"canonical_admission": checkpoint, "proof_sha256": hashlib.sha256(canonical(actual)).hexdigest(),
            "guard_identity": [metadata.st_dev, metadata.st_ino], "binding": actual["binding"],
            "stage_output": actual["stage_output"], "status": actual["status"]}
    return _CompleteClosureProof(current, binding, digest(current["effects"]),
        tuple(positive_gets), producer, window.projection(), window.deadline, _CLOSURE_SEAL)


def public_closure(value):
    if not value:
        return None
    closure = json.loads(value) if isinstance(value, str) else value
    return {key: closure[key] for key in ("closure_id", "closed_at", "artifact_id",
        "artifact_sha256", "native_kind", "observation_only")}
