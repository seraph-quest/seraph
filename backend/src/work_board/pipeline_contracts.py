"""Closed contracts for one reviewed public evidence chain; no DAG runtime."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PIPELINE_KIND = "public-evidence-pipeline.v1"
DOSSIER = "work.evidence-dossier.v1"
REPORT = "work.local-evidence-report.v1"
CPU_KINDS = frozenset({DOSSIER, REPORT})
SLOTS = ("public_source", "evidence_dossier", "local_report")
CAPABILITIES = ("browser.public-task.v1", DOSSIER, REPORT)
MAX_OUTPUT_BYTES = 64 * 1024
MAX_QUOTED_BYTES = 40 * 1024


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


class EvidenceConsumerInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1]
    operation_ref: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:-]+$")
    plan_version: int = Field(ge=1)
    producer_task_ref: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:-]+$")
    producer_attempt_ref: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:-]+$")
    handoff_ref: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:-]+$")
    producer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    producer_schema: Literal["browser_public_task_result", "evidence_dossier.v1"]
    quoted_source_data: str = Field(min_length=1)
    no_learning: Literal[True]

    @field_validator("quoted_source_data")
    @classmethod
    def bounded_quoted_data(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_QUOTED_BYTES:
            raise ValueError("quoted source data exceeds the finite input allowance")
        return value


class PipelinePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    source_input_artifact_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:-]+$")
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:-]+$")


class PipelineAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    expected_parent_revision: int = Field(ge=1)
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PipelineRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    source_input_artifact_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:-]+$")
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:-]+$")


class PipelineReuseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
    expected_parent_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:-]+$")


class PipelineAdvanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=1)
