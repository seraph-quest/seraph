"""Closed operator input for one pre-reviewed formatter, never code or argv."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from src.execution.tool_package_profile import expected_output


class JsonFormatInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal[1]
    json_text: str = Field(min_length=1, max_length=32768)
    no_learning: Literal[True]

    @field_validator("json_text")
    @classmethod
    def finite_json(cls, value):
        expected_output(value.encode("utf-8"))
        return value


class ToolPackageRecoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
