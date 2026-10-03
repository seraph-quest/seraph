"""Closed input/output grammar for the fixed depth-one research capability."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PARENT_CAPABILITY = "work.research-dossier.v1"
CHILD_CAPABILITY = "work.readonly-research-child.v1"
PARENT_KIND = "research_dossier"
CHILD_KIND = "readonly_research_child"
WAIT_SOURCES = "research_wait_sources"
WAIT_CHILDREN = "research_wait_children"
PROMPT_READY = "research_prompt_ready"
PARENT_SECONDS = 300
CHILD_SECONDS = 120
CONTACT_SECONDS = 45
PROMPT_BYTES = 8192
SOURCE_BYTES = 64 * 1024
CHILD_OUTPUT_BYTES = 16 * 1024
OUTPUT_BYTES = 64 * 1024


class ClosedResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ResearchControlRequest(ClosedResearchModel):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:-]+$")


class PublicTextSource(ClosedResearchModel):
    kind: Literal["public_https_text"]
    url: str = Field(min_length=1, max_length=2048)
    first_line: int = Field(ge=1, le=65536)
    last_line: int = Field(ge=1, le=65536)

    @field_validator("url")
    @classmethod
    def exact_public_https(cls, value):
        from src.browser.pinned_transport import parse_public_https_url
        parse_public_https_url(value)
        return value

    @model_validator(mode="after")
    def finite_span(self):
        if self.last_line < self.first_line:
            raise ValueError("source line span is reversed")
        return self


class LocalArtifactSource(ClosedResearchModel):
    kind: Literal["completed_board_artifact"]
    producer_task_ref: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:-]+$")
    producer_attempt_ref: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:-]+$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    first_line: int = Field(ge=1, le=65536)
    last_line: int = Field(ge=1, le=65536)

    @model_validator(mode="after")
    def finite_span(self):
        if self.last_line < self.first_line:
            raise ValueError("source line span is reversed")
        return self


Source = Annotated[PublicTextSource | LocalArtifactSource, Field(discriminator="kind")]


class ResearchPerspective(ClosedResearchModel):
    instruction: str = Field(min_length=1, max_length=1024)
    source_slots: list[int] = Field(min_length=1, max_length=2)

    @field_validator("instruction")
    @classmethod
    def bounded_instruction(cls, value):
        if len(value.encode("utf-8")) > 1024:
            raise ValueError("perspective exceeds 1 KiB UTF-8")
        return value

    @field_validator("source_slots")
    @classmethod
    def unique_slots(cls, value):
        if len(set(value)) != len(value) or any(type(slot) is not int or not 0 <= slot < 4 for slot in value):
            raise ValueError("only unique fixed source slots are permitted")
        return value


class ResearchDossierInput(ClosedResearchModel):
    schema_version: Literal[1]
    question: str = Field(min_length=1, max_length=2048)
    perspectives: list[ResearchPerspective] = Field(min_length=1, max_length=2)
    sources: list[Source] = Field(min_length=1, max_length=4)
    # Operator source selection and model-egress consent are separate acts.
    source_egress_acknowledged: Literal[True]
    no_learning: Literal[True]

    @field_validator("question")
    @classmethod
    def bounded_question(cls, value):
        if len(value.encode("utf-8")) > 2048:
            raise ValueError("question exceeds 2 KiB UTF-8")
        return value

    @model_validator(mode="after")
    def fixed_assignments(self):
        used = {slot for perspective in self.perspectives for slot in perspective.source_slots}
        if used != set(range(len(self.sources))):
            raise ValueError("every declared source must belong to a fixed child slot")
        identities = [source.url if isinstance(source, PublicTextSource) else (source.producer_task_ref, source.producer_attempt_ref, source.source_sha256) for source in self.sources]
        if len(set(identities)) != len(identities):
            raise ValueError("declare a shared source once and reuse its fixed slot")
        return self


class ResearchCitation(ClosedResearchModel):
    source_id: str = Field(pattern=r"^source:[0-3]$")
    first_line: int = Field(ge=1, le=65536)
    last_line: int = Field(ge=1, le=65536)
    span_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResearchClaim(ClosedResearchModel):
    text: str = Field(min_length=1, max_length=512)
    citations: list[ResearchCitation] = Field(max_length=2)

    @field_validator("text")
    @classmethod
    def bounded_text(cls, value):
        if len(value.encode("utf-8")) > 512:
            raise ValueError("claim exceeds its finite UTF-8 allowance")
        return value


class ResearchChildOutput(ClosedResearchModel):
    schema_version: Literal[1]
    perspective: str = Field(min_length=1, max_length=1024)
    claims: list[ResearchClaim] = Field(max_length=8)
    uncertainty: list[str] = Field(max_length=8)
    contradictions: list[str] = Field(max_length=8)
    no_learning: Literal[True]

    @field_validator("perspective")
    @classmethod
    def bounded_perspective(cls, value):
        if len(value.encode("utf-8")) > 1024:
            raise ValueError("perspective exceeds its finite allowance")
        return value

    @field_validator("uncertainty", "contradictions")
    @classmethod
    def bounded_notes(cls, value):
        if any(not item or len(item.encode("utf-8")) > 512 for item in value):
            raise ValueError("research note exceeds its finite allowance")
        return value
