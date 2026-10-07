"""Closed private input and content-safe receipts for fixed NEAR HTTPS text."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator


class NearTextError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class NearTextInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["seraph.near.text.input.v1"]
    question: StrictStr = Field(min_length=1, repr=False)
    max_output_tokens: StrictInt = Field(ge=1, le=1024)

    @field_validator("question")
    @classmethod
    def bounded_question(cls, value: str) -> str:
        if not 1 <= len(value.encode("utf-8")) <= 8192 or not value.strip():
            raise ValueError("near_text_question_invalid")
        return value


class NearTextReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["seraph.near.text.receipt.v1"] = "seraph.near.text.receipt.v1"
    task_id: StrictStr | None = None
    attempt_id: StrictStr | None = None
    job_id: StrictStr
    request_id: StrictStr
    operation_id: StrictStr
    provider: Literal["near"] = "near"
    profile_id: Literal["near.text"] = "near.text"
    model_id: Literal["z-ai/glm-5.3-flash"] = "z-ai/glm-5.3-flash"
    api_base: Literal["https://cloud-api.near.ai/v1"] = "https://cloud-api.near.ai/v1"
    tls_transport: Literal[True] = True
    tee_verified: Literal[False] = False
    e2ee: Literal[False] = False
    input_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    output_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    billing_response_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    provider_request_id: StrictStr
    cost_source: Literal["near_billing_costs"] = "near_billing_costs"
    cost_nano_usd: StrictInt = Field(ge=0, le=1_000_000_000_000)
    cost_microusd: StrictInt = Field(ge=0, le=1_000_000_000)
    cost_state: Literal["settled"] = "settled"
    cost_reference: StrictStr
    memory_status: Literal["no_learning"] = "no_learning"


@dataclass(frozen=True)
class NearTextAnswer:
    text: str = field(repr=False)
    receipt: NearTextReceipt
