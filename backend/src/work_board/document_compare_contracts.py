"""Closed references-only document input; raw sources use separate streams."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class DocumentSourceDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    size_bytes: int = Field(ge=1, le=2 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentPairReserve(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    operation: Literal["compare-line-totals-by-sku"]
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    pdf: DocumentSourceDescriptor
    csv: DocumentSourceDescriptor
    no_learning: Literal[True]


class DocumentCompareInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    operation: Literal["compare-line-totals-by-sku"]
    pair_ref: str = Field(pattern=r"^document-pair:[0-9a-f-]{36}$")
    pdf: DocumentSourceDescriptor
    csv: DocumentSourceDescriptor
    no_learning: Literal[True]


class DocumentPairMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    expected_revision: int = Field(ge=1)
