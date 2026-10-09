"""Closed v3 audio evidence and output contracts; no inference or proof minting."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import base64
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

_DIGEST = re.compile(r"[0-9a-f]{64}")
_ENDPOINT = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}(?:/[a-z0-9][a-z0-9_.:-]{0,63})?")


def validate_exact_audio_endpoint_slug(value: str) -> str:
    if type(value) is not str or len(value) > 128 or not value.isascii() or _ENDPOINT.fullmatch(value) is None:
        raise ValueError("audio_endpoint_not_exact")
    return value


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("observed_at", "expires_at", check_fields=False)
    @classmethod
    def utc_time(cls, value):
        if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
            raise ValueError("audio_witness_time_requires_utc")
        return value

    @field_validator("model_id", check_fields=False)
    @classmethod
    def qualified_model(cls, value):
        from .configuration import normalize_openrouter_model_id
        if normalize_openrouter_model_id(value) != value:
            raise ValueError("audio_model_not_canonical")
        return value

    @field_validator("upstream_endpoint_tag", check_fields=False)
    @classmethod
    def exact_endpoint(cls, value):
        return validate_exact_audio_endpoint_slug(value)

    @model_validator(mode="after")
    def freshness_bounds(self):
        if hasattr(self, "observed_at"):
            delta = (self.expires_at - self.observed_at).total_seconds()
            if self.observed_at > datetime.now(timezone.utc) or not 0 < delta <= 86400:
                raise ValueError("audio_witness_expiry_invalid")
        return self


Digest = str


class AudioEndpointWitnessV1(_Closed):
    schema_version: Literal["audio-endpoint-witness.v1"]
    profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(max_length=256)
    upstream_endpoint_tag: str
    api_kind: Literal["chat_completions"]
    input_format: Literal["wav", "webm", "m4a"]
    container: Literal["wav", "webm", "mp4"]
    codec: Literal["pcm_s16le", "opus", "aac"]
    output_kind: Literal["text"]
    max_duration_millis: int = Field(strict=True, ge=1, le=60000)
    max_input_bytes: int = Field(strict=True, ge=1, le=10485760)
    max_output_tokens: int = Field(strict=True, ge=1, le=8192)
    metadata_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    format_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pricing_witness_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def format_triple(self):
        if (self.container, self.codec, self.input_format) not in {("wav", "pcm_s16le", "wav"), ("webm", "opus", "webm"), ("mp4", "aac", "m4a")}:
            raise ValueError("audio_format_unverified")
        if self.input_format == "wav" and self.max_input_bytes > 2097152:
            raise ValueError("audio_wav_bound_exceeded")
        return self


class AudioPricingWitnessV1(_Closed):
    schema_version: Literal["audio-pricing-witness.v1"]
    model_id: str = Field(max_length=256)
    upstream_endpoint_tag: str
    api_kind: Literal["chat_completions"]
    pricing_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    billing_units: tuple[Literal["request", "input_token", "output_token", "input_audio_second", "input_audio_token"], ...]
    unit_rates_microusd: dict[str, str]
    maximum_billable_units: dict[str, StrictInt | StrictStr]
    reserve_microusd: int = Field(strict=True, ge=1, le=1000000000)
    observed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def finite_pricing(self):
        keys = set(self.billing_units)
        if not 1 <= len(keys) <= 5 or len(keys) != len(self.billing_units) or keys != set(self.unit_rates_microusd) or keys != set(self.maximum_billable_units):
            raise ValueError("audio_pricing_unbounded")
        # 64-character rates plus 16-digit maxima must not round downward
        # under the ambient Decimal context before the final upward rounding.
        total = Decimal(0)
        for unit in self.billing_units:
            rate = canonical_decimal(self.unit_rates_microusd[unit], maximum=Decimal(1000000000))
            maximum = self.maximum_billable_units[unit]
            if unit == "input_audio_second":
                amount = canonical_decimal(maximum, maximum=Decimal(60))
                if amount <= 0:
                    raise ValueError("audio_pricing_unbounded")
            else:
                if type(maximum) is not int or not 1 <= maximum <= 9007199254740991 or unit == "request" and maximum != 1:
                    raise ValueError("audio_pricing_unbounded")
                amount = Decimal(maximum)
            with localcontext() as precision:
                precision.prec = 128
                total += rate * amount
        if max(1, int(total.to_integral_value(rounding=ROUND_CEILING))) != self.reserve_microusd:
            raise ValueError("audio_pricing_reserve_mismatch")
        return self


def canonical_decimal(value, *, maximum: Decimal) -> Decimal:
    if type(value) is not str or len(value) > 64 or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?", value) is None:
        raise ValueError("audio_pricing_decimal_invalid")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("audio_pricing_decimal_invalid") from exc
    if not result.is_finite() or not 0 <= result <= maximum:
        raise ValueError("audio_pricing_unbounded")
    return result


class AudioOfficialEvidenceV1(_Closed):
    """Repository documentary evidence; fixtures never satisfy route readiness.

    Source documents are referenced by digest, never URLs supplied to transport.
    Publication remains the existing proof/receipt owner's responsibility.
    """
    schema_version: Literal["audio-official-evidence.v1"]
    origin: Literal["official_documentation"]
    endpoint: AudioEndpointWitnessV1
    pricing: AudioPricingWitnessV1
    exact_endpoint_identity_attested: bool = Field(strict=True)
    all_charge_units_bounded_attested: bool = Field(strict=True)
    format_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pricing_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def linked(self):
        if self.exact_endpoint_identity_attested is not True or self.all_charge_units_bounded_attested is not True:
            raise ValueError("audio_pricing_unbounded")
        if self.endpoint.model_id != self.pricing.model_id or self.endpoint.upstream_endpoint_tag != self.pricing.upstream_endpoint_tag:
            raise ValueError("audio_endpoint_not_exact")
        if self.endpoint.pricing_witness_digest != witness_digest(self.pricing) or self.endpoint.format_evidence_digest != self.format_source_digest or self.pricing.pricing_digest != self.pricing_source_digest:
            raise ValueError("audio_witness_digest_invalid")
        return self


def witness_digest(value: BaseModel) -> str:
    return hashlib.sha256(json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_audio_route_proof(proof, profile, *, now=None):
    from .proofs import proof_is_fresh
    if not proof_is_fresh(proof, now=now) or proof.capability != "audio_input" or proof.profile_contract_hash != profile.contract_hash or proof.profile_id != "openrouter.audio":
        raise ValueError("audio_format_unverified")
    if type(proof.proven_value) is not str or len(proof.proven_value.encode("utf-8")) > 16384:
        raise ValueError("audio_format_unverified")
    evidence = AudioOfficialEvidenceV1.model_validate_json(proof.proven_value)
    point = datetime.fromtimestamp(now, timezone.utc) if now is not None else datetime.now(timezone.utc)
    controls = profile.options.get("provider", {})
    endpoint = evidence.endpoint
    if endpoint.profile_hash != profile.contract_hash or endpoint.model_id != (profile.routing_model or profile.model) or controls.get("only") != [endpoint.upstream_endpoint_tag]:
        raise ValueError("audio_endpoint_not_exact")
    if any(not item.observed_at <= point < item.expires_at or item.expires_at.timestamp() > proof.expires_at for item in (endpoint, evidence.pricing)):
        raise ValueError("audio_witness_expired")
    if "output_token" in evidence.pricing.billing_units and evidence.pricing.maximum_billable_units["output_token"] < profile.max_output_tokens:
        raise ValueError("audio_pricing_unbounded")
    if "input_audio_second" in evidence.pricing.billing_units and Decimal(evidence.pricing.maximum_billable_units["input_audio_second"]) * 1000 < endpoint.max_duration_millis:
        raise ValueError("audio_pricing_unbounded")
    if evidence.pricing.reserve_microusd > profile.options.get("_seraph_openrouter", {}).get("request_cost_bound_microusd", 0) or profile.max_output_tokens > endpoint.max_output_tokens:
        raise ValueError("audio_pricing_unbounded")
    return evidence


async def audio_route_witness(profile, proof_ref=None):
    from .repository import model_fabric_repository
    from .selector import candidate_from_profile
    candidate = candidate_from_profile(profile)
    proof = await model_fabric_repository.latest_capability_proof(profile_schema_version=profile.schema_version,
        profile_contract_hash=profile.contract_hash, profile_id=profile.id, model=profile.model,
        endpoint=candidate.endpoint, endpoint_class=candidate.endpoint_class, adapter=candidate.adapter,
        capability="audio_input")
    if proof is None or proof_ref is not None and proof_ref != proof.proof_hash:
        raise PermissionError("audio_format_unverified")
    try:
        validate_audio_route_proof(proof, profile)
        # A literal provenance label and lookalike proof are not an issuer.
        # The current repository has only empirical acquisition; no adopted
        # documentary attestation action exists yet. Never mint authority from
        # this structural parser while that production owner is unavailable.
        raise PermissionError("audio_documentary_acquisition_unavailable")
    except (ValueError, TypeError) as exc:
        raise PermissionError("audio_format_unverified") from exc


def input_audio_payload(data: str, format: str, witness: AudioEndpointWitnessV1):
    if type(data) is not str or format != witness.input_format or len(data) > 4 * ((witness.max_input_bytes + 2) // 3):
        raise ValueError("audio_input_invalid")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("audio_input_invalid") from exc
    if not decoded or len(decoded) > witness.max_input_bytes or base64.b64encode(decoded).decode("ascii") != data:
        raise ValueError("audio_input_invalid")
    return {"type": "input_audio", "input_audio": {"data": data, "format": format}}


def validate_audio_response(payload, *, model_id: str, upstream: str):
    from .configuration import normalize_openrouter_model_id
    if not isinstance(payload, dict) or normalize_openrouter_model_id(payload.get("model", "")) != model_id or payload.get("provider") != upstream:
        raise ValueError("audio_response_identity_invalid")
    generation = payload.get("id")
    if type(generation) is not str or re.fullmatch(r"gen-[A-Za-z0-9_-]{1,124}", generation) is None:
        raise ValueError("audio_response_identity_invalid")
    if {"audio", "audio_chunks", "modalities", "delta", "tool_calls", "function_call", "refusal"}.intersection(payload):
        raise ValueError("audio_response_invalid")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("audio_response_invalid")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop" or set(choice) - {"index", "finish_reason", "native_finish_reason", "message", "logprobs"}:
        raise ValueError("audio_response_invalid")
    if choice.get("native_finish_reason", "stop") != "stop" or "index" in choice and (type(choice["index"]) is not int or choice["index"] != 0):
        raise ValueError("audio_response_invalid")
    message = choice.get("message")
    if not isinstance(message, dict) or set(message) - {"role", "content"} or message.get("role") != "assistant":
        raise ValueError("audio_response_invalid")
    content = message.get("content")
    if type(content) is not str or not content.strip() or "\x00" in content or len(content) > 20000 or len(content.encode()) > 80000:
        raise ValueError("audio_response_invalid")
    usage = payload.get("usage")
    if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
        raise ValueError("audio_response_billing_invalid")
    if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
        raise ValueError("audio_response_billing_invalid")
    cost = usage.get("cost")
    if type(cost) not in (int, float, str, Decimal) or type(cost) is bool:
        raise ValueError("audio_response_billing_invalid")
    if len(str(cost)) > 64:
        raise ValueError("audio_response_billing_invalid")
    try:
        exact_cost = Decimal(str(cost))
        parts = exact_cost.as_tuple()
        if not exact_cost.is_finite() or len(parts.digits) > 64 or not -64 <= parts.exponent <= 3:
            raise ValueError("audio_response_billing_invalid")
        with localcontext() as precision:
            precision.prec = 128
            money = exact_cost * Decimal(1000000)
    except InvalidOperation as exc:
        raise ValueError("audio_response_billing_invalid") from exc
    if not money.is_finite() or not 0 <= money <= 1000000000:
        raise ValueError("audio_response_billing_invalid")
    from src.workflows.inference_accounting import account_charge_microusd
    authoritative_cost, authoritative_generation = account_charge_microusd(payload)
    if authoritative_cost is None or authoritative_generation != generation:
        raise ValueError("audio_response_billing_invalid")
    return content, generation, authoritative_cost
