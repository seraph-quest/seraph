"""Private contracts for the two accepted finite GitHub capacity-close routes.

Requests contain locators and exact CAS identities only. Positive effect proof
is minted from the existing protected adapter and trusted fixed producer.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
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


def original_effects(current):
    """Exact complete canonical inventory; diagnostic observations add no intent."""
    effects = current.get("effects")
    if not isinstance(effects, list) or not 1 <= len(effects) <= 4096:
        raise ValueError("github_capacity_effect_inventory_missing")
    ids = set()
    original = []
    for effect in effects:
        if isinstance(effect, dict) and effect.get("kind") == "approval_resume" and effect.get("status") == "approved" and effect.get("authority_digest") == current.get("authority_digest") and effect.get("approval_id") == current.get("declared_authority", {}).get("approval_id"):
            continue
        if not isinstance(effect, dict) or not isinstance(effect.get("effect_id"), str) or effect["effect_id"] in ids:
            raise ValueError("github_capacity_effect_inventory_invalid")
        ids.add(effect["effect_id"])
        details = effect.get("details") or {}
        if details.get("observation_only") is True or details.get("readback_observation_only") is True or effect.get("original_effect_id"):
            continue
        kind = effect.get("effect_type")
        if current["job_kind"] == "github_followthrough_v1":
            if kind == "github_publication":
                original.append(effect)
            elif kind is not None and effect.get("status") in {"intent", "dispatched", "unknown", "unknown_external_effect"}:
                raise ValueError("github_capacity_unproved_other_intent")
        elif current["job_kind"] == "engineering.repo-publication.v1":
            if kind in {"repo_publication_local_producer", "repo_publication_tree", "repo_publication_commit", "repo_publication_branch", "repo_publication_pr"} or isinstance(kind, str) and re.fullmatch(r"repo_publication_blob_[0-9a-f]{16}", kind):
                original.append(effect)
            elif kind is not None and effect.get("status") in {"intent", "dispatched", "unknown", "unknown_external_effect"}:
                raise ValueError("github_capacity_unproved_other_intent")
        else:
            raise ValueError("github_capacity_native_kind_invalid")
    if not original or current["job_kind"] == "github_followthrough_v1" and len(original) != 1:
        raise ValueError("github_capacity_effect_inventory_missing")
    return original


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
