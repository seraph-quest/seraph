"""NEAR TEE catalog check and content-safe hosting receipts (ADR-035).

The check reads only ``GET https://cloud-api.near.ai/v1/model/{id}`` for each
enabled slot's canonical model. It makes no inference, sends no operator
content and no credential (no source evidence shows the endpoint requires
one). Settings GET/PUT never call it; dispatch never refreshes it.

A passing receipt is an additional hosting gate, not a capability proof and
not a confidentiality claim: "TEE-hosted per NEAR catalog as of <time>; NEAR
gateway receives plaintext; attestation not verified by Seraph; no end-to-end
encryption".

Parsed catalog shape: the real record the lead read unauthenticated on
2026-11-13 (e.g. ``/v1/model/z-ai%2Fglm-5.3-flash``). Anything else fails
closed; metadata fields are read only from ``metadata`` (no top-level
fallback):

* top level: ``modelId`` (must equal the canonical id) and
  ``inputCostPerToken`` / ``outputCostPerToken`` as
  ``{"amount": int, "scale": int, "currency": "USD"}`` (USD per token =
  amount x 10^-scale; e.g. glm-5.3-flash input ``{80, 9}`` = $0.08/M);
* optional top-level ``costPerImage`` (same price shape, USD per image); the
  vision slot adds it once per request to the cost bound (one image per
  request) and fails closed when it is malformed;
* ``metadata``: ``providerType``, ``attestationSupported``, ``verifiable``,
  ``contextLength``, ``architecture.inputModalities`` /
  ``architecture.outputModalities``, ``supportedFeatures``, optional
  ``maxOutputLength`` and optional ``isReady``.

Readiness absent (as for ``deepseek-ai/DeepSeek-V4.1-Flash-1017``) is not
ready. An absent ``maxOutputLength`` is an unknown output limit: the slot then
passes only with ``max_output_tokens`` at most
``UNKNOWN_OUTPUT_LIMIT_CAP`` (8192), otherwise
``near_tee_catalog_output_limit_unknown``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import quote

from config.settings import settings


CATALOG_RECEIPT_SCHEMA = "seraph.near.tee.catalog.receipt.v1"
CATALOG_STORE_SCHEMA = "seraph.near.tee.catalog.receipts.v1"
CATALOG_RESPONSE_CAP = 65536
CATALOG_TIMEOUT_SECONDS = 10.0
RECEIPT_STALE = "near_tee_catalog_receipt_stale"
RECEIPT_MISSING = "near_tee_catalog_receipt_missing"
TEE_LABEL_TEMPLATE = ("TEE-hosted per NEAR catalog as of {as_of}; NEAR gateway receives plaintext; "
    "attestation not verified by Seraph; no end-to-end encryption")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
# The modalities each implemented slot needs from the catalog record. The
# embedding row's per-model output modality was not read by the lead
# (ADR-035): either spelling of the embedding modality is accepted; anything
# else fails closed with ``near_tee_catalog_modality_missing``.
_SLOT_MODALITIES = {"text": ({"text"}, {"text"}), "vision": ({"text", "image"}, {"text"})}
_EMBEDDING_OUTPUT_MODALITIES = frozenset({"embedding", "embeddings"})
# Optional catalog fields that may state a vector dimension. Recorded when
# present (a strict integer); the capability check measures the dimension in
# every case and a disagreement blocks the namespace.
_EMBEDDING_DIMENSION_FIELDS = ("embeddingDimension", "embeddingDimensions", "dimensions", "outputDimension")
# Conservative output bound when the catalog omits maxOutputLength.
UNKNOWN_OUTPUT_LIMIT_CAP = 8192


class NearTeeCatalogError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CatalogFacts:
    model_id: str
    provider_type: str | None
    attestation_supported: bool | None
    verifiable: bool | None
    is_ready: bool | None
    context_length: int | None
    max_output_tokens: int | None
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    features: tuple[str, ...]
    input_price: tuple[int, int] | None
    output_price: tuple[int, int] | None
    image_price: tuple[int, int] | None = None
    image_price_invalid: bool = False
    embedding_dimension: int | None = None


def _store_path() -> Path:
    return Path(settings.workspace_dir).expanduser().resolve() / "near-tee-catalog-receipts.json"


def _safe_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 32:
        raise NearTeeCatalogError("near_tee_catalog_response_invalid")
    items = []
    for item in value:
        if not isinstance(item, str) or _SAFE_TOKEN.fullmatch(item) is None:
            raise NearTeeCatalogError("near_tee_catalog_response_invalid")
        items.append(item)
    return tuple(sorted(set(items)))


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise NearTeeCatalogError("near_tee_catalog_response_invalid")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 < value <= 100_000_000:
        raise NearTeeCatalogError("near_tee_catalog_response_invalid")
    return value


def _price(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    if (not isinstance(value, dict) or type(value.get("amount")) is not int or type(value.get("scale")) is not int
            or value.get("currency", "USD") != "USD" or not 0 <= value["amount"] <= 10**15
            or not 0 <= value["scale"] <= 30):
        raise NearTeeCatalogError("near_tee_catalog_response_invalid")
    return value["amount"], value["scale"]


def parse_catalog_record(raw: bytes, *, model_id: str) -> CatalogFacts:
    from .near_client import strict_json_object
    try:
        payload = strict_json_object(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise NearTeeCatalogError("near_tee_catalog_response_invalid") from None
    if not isinstance(payload, dict):
        raise NearTeeCatalogError("near_tee_catalog_response_invalid")
    if payload.get("modelId") != model_id:
        raise NearTeeCatalogError("near_tee_catalog_model_mismatch")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise NearTeeCatalogError("near_tee_catalog_response_invalid")
    architecture = metadata.get("architecture")
    if not isinstance(architecture, dict):
        # The older flat modality layout is not the catalog's shape: fail closed.
        raise NearTeeCatalogError("near_tee_catalog_response_invalid")
    provider_type = metadata.get("providerType")
    if provider_type is not None and (not isinstance(provider_type, str) or _SAFE_TOKEN.fullmatch(provider_type) is None):
        raise NearTeeCatalogError("near_tee_catalog_response_invalid")
    try:
        image_price, image_price_invalid = _price(payload.get("costPerImage")), False
    except NearTeeCatalogError:
        # Only the vision slot prices images; it fails closed on this flag.
        image_price, image_price_invalid = None, True
    dimension = None
    for name in _EMBEDDING_DIMENSION_FIELDS:
        if metadata.get(name) is not None:
            value = _optional_int(metadata.get(name))
            if value > 65_536 or dimension not in (None, value):
                raise NearTeeCatalogError("near_tee_catalog_response_invalid")
            dimension = value
    return CatalogFacts(model_id=model_id, provider_type=provider_type, embedding_dimension=dimension,
        attestation_supported=_optional_bool(metadata.get("attestationSupported")),
        verifiable=_optional_bool(metadata.get("verifiable")), is_ready=_optional_bool(metadata.get("isReady")),
        context_length=_optional_int(metadata.get("contextLength")),
        max_output_tokens=_optional_int(metadata.get("maxOutputLength")),
        input_modalities=_safe_list(architecture.get("inputModalities")),
        output_modalities=_safe_list(architecture.get("outputModalities")),
        features=_safe_list(metadata.get("supportedFeatures")),
        input_price=_price(payload.get("inputCostPerToken")),
        output_price=_price(payload.get("outputCostPerToken")),
        image_price=image_price, image_price_invalid=image_price_invalid)


def required_cost_microusd(facts: CatalogFacts, *, max_input_tokens: int, max_output_tokens: int,
                           images: int = 0) -> int | None:
    """Exact ceiling of max_in x input price + max_out x output price
    (+ images x per-image price when the catalog lists one), in microUSD."""
    if facts.input_price is None or facts.output_price is None:
        return None
    total = 0
    from fractions import Fraction
    terms = [(max_input_tokens, facts.input_price), (max_output_tokens, facts.output_price)]
    if images and facts.image_price is not None:
        terms.append((images, facts.image_price))
    for units, (amount, scale) in terms:
        total += Fraction(units * amount * 1_000_000, 10 ** scale)
    return math.ceil(total)


def _evaluate_embedding(facts: CatalogFacts, *, slot) -> tuple[str | None, int | None]:
    """Embedding slot catalog rules.

    The catalog lists an output price for the embedding model, but no source
    states how NEAR bills embedding output (per vector, per dimension or not
    at all). The bound therefore includes ``max_output_tokens`` (fixed at 1)
    x the output price as well as the input; the reserve is a local admission
    hold and an over-bound genuine charge still goes to overrun review.
    """
    required = required_cost_microusd(facts, max_input_tokens=slot.max_input_tokens,
        max_output_tokens=slot.max_output_tokens)
    if facts.provider_type != "vllm":
        return "near_tee_catalog_provider_not_vllm", required
    if facts.attestation_supported is not True:
        return "near_tee_catalog_not_attested", required
    if facts.verifiable is not True:
        return "near_tee_catalog_not_verifiable", required
    if facts.is_ready is not True:
        return "near_tee_catalog_not_ready", required
    if "text" not in facts.input_modalities or not _EMBEDDING_OUTPUT_MODALITIES.intersection(facts.output_modalities):
        return "near_tee_catalog_modality_missing", required
    if facts.context_length is None or facts.context_length < slot.max_input_tokens:
        return "near_tee_catalog_context_too_small", required
    if required is None:
        return "near_tee_catalog_price_missing", required
    if slot.request_cost_bound_microusd < required:
        return "near_tee_catalog_cost_bound_too_small", required
    return None, required


def evaluate_catalog(facts: CatalogFacts, *, slot_name: str, slot) -> tuple[str | None, int | None]:
    """Return (failure reason or None, required cost microUSD)."""
    if slot_name == "embedding":
        return _evaluate_embedding(facts, slot=slot)
    needed_in, needed_out = _SLOT_MODALITIES[slot_name]
    images = 0
    if slot_name == "vision":
        from .configuration import NEAR_TEE_VISION_MAX_IMAGES_PER_REQUEST
        images = NEAR_TEE_VISION_MAX_IMAGES_PER_REQUEST
    required = required_cost_microusd(facts, max_input_tokens=slot.max_input_tokens,
        max_output_tokens=slot.max_output_tokens, images=images)
    if facts.provider_type != "vllm":
        return "near_tee_catalog_provider_not_vllm", required
    if facts.attestation_supported is not True:
        return "near_tee_catalog_not_attested", required
    if facts.verifiable is not True:
        return "near_tee_catalog_not_verifiable", required
    if facts.is_ready is not True:
        return "near_tee_catalog_not_ready", required
    if not needed_in <= set(facts.input_modalities) or not needed_out <= set(facts.output_modalities):
        return "near_tee_catalog_modality_missing", required
    if facts.context_length is None or facts.context_length < slot.max_input_tokens:
        return "near_tee_catalog_context_too_small", required
    if facts.max_output_tokens is None:
        if slot.max_output_tokens > UNKNOWN_OUTPUT_LIMIT_CAP:
            return "near_tee_catalog_output_limit_unknown", required
    elif facts.max_output_tokens < slot.max_output_tokens:
        return "near_tee_catalog_output_too_small", required
    if images and facts.image_price_invalid:
        return "near_tee_catalog_response_invalid", required
    if required is None:
        return "near_tee_catalog_price_missing", required
    if slot.request_cost_bound_microusd < required:
        return "near_tee_catalog_cost_bound_too_small", required
    return None, required


def _price_text(price: tuple[int, int] | None) -> str | None:
    if price is None:
        return None
    from decimal import Decimal
    return format(Decimal(price[0]).scaleb(-price[1]) * 1_000_000, "f")


def _image_price_text(price: tuple[int, int] | None) -> str | None:
    if price is None:
        return None
    from decimal import Decimal
    return format(Decimal(price[0]).scaleb(-price[1]), "f")


def receipt_structured_outputs(slot_name: str, *, receipts: dict[str, dict[str, object]] | None = None) -> bool:
    """Whether the slot's stored receipt lists NEAR's ``structured_outputs`` feature.

    Read only after ``catalog_receipt_block_reason`` passed for the same
    receipts; never refreshes the catalog.
    """
    receipt = (read_catalog_receipts() if receipts is None else receipts).get(slot_name)
    features = receipt.get("features") if isinstance(receipt, dict) else None
    return isinstance(features, list) and "structured_outputs" in features


def build_receipt(*, slot_name: str, slot, profile, facts: CatalogFacts | None, raw: bytes | None,
                  reason: str | None, required: int | None, revision: int, now: float) -> dict[str, object]:
    ttl = int(slot.catalog_ttl_hours) * 3600
    return {
        "schema_version": CATALOG_RECEIPT_SCHEMA, "slot": slot_name, "profile_id": profile.id,
        "model_id": slot.model_id, "profile_hash": profile.contract_hash,
        "configuration_revision": revision, "checked_at": now,
        "checked_at_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "expires_at": now + ttl, "ttl_hours": int(slot.catalog_ttl_hours),
        "outcome": "passed" if reason is None else "failed", "reason_code": reason,
        "provider_type": facts.provider_type if facts else None,
        "attestation_supported": facts.attestation_supported if facts else None,
        "verifiable": facts.verifiable if facts else None,
        "is_ready": facts.is_ready if facts else None,
        "input_modalities": list(facts.input_modalities) if facts else [],
        "output_modalities": list(facts.output_modalities) if facts else [],
        "features": list(facts.features) if facts else [],
        "input_price_usd_per_million": _price_text(facts.input_price) if facts else None,
        "output_price_usd_per_million": _price_text(facts.output_price) if facts else None,
        # USD per image (not per million); recorded for the vision slot only.
        "image_price_usd": (_image_price_text(facts.image_price) if facts and slot_name == "vision" else None),
        "structured_outputs": bool(facts and "structured_outputs" in facts.features),
        "context_length": facts.context_length if facts else None,
        "max_output_tokens": facts.max_output_tokens if facts else None,
        "catalog_embedding_dimension": facts.embedding_dimension if facts else None,
        "required_cost_microusd": required,
        "request_cost_bound_microusd": slot.request_cost_bound_microusd,
        "response_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        "tee_hosted_per_catalog": reason is None, "tee_verified": False, "e2ee": False,
        "provider_receives_plaintext": True,
    }


def read_catalog_receipts() -> dict[str, dict[str, object]]:
    path = _store_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != CATALOG_STORE_SCHEMA:
        return {}
    receipts = payload.get("receipts")
    return {slot: item for slot, item in receipts.items()
        if isinstance(item, dict) and item.get("schema_version") == CATALOG_RECEIPT_SCHEMA} if isinstance(receipts, dict) else {}


def write_catalog_receipts(receipts: dict[str, dict[str, object]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump({"schema_version": CATALOG_STORE_SCHEMA, "receipts": receipts}, handle,
                sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def catalog_receipt_block_reason(slot_name: str, profile, *, now: float | None = None,
                                 receipts: dict[str, dict[str, object]] | None = None,
                                 revision: int | None = None) -> str | None:
    """Local precontact re-validation; never refreshes the catalog.

    A receipt is current only for the same canonical model, complete profile
    hash and configuration (egress) revision, and before its expiry.
    """
    checked = time.time() if now is None else now
    if revision is None:
        from .configuration import read_model_fabric_configuration
        revision = read_model_fabric_configuration().egress_revision
    receipt = (read_catalog_receipts() if receipts is None else receipts).get(slot_name)
    if receipt is None:
        return RECEIPT_MISSING
    if receipt.get("model_id") != profile.model or receipt.get("profile_hash") != profile.contract_hash \
            or receipt.get("profile_id") != profile.id or receipt.get("configuration_revision") != revision:
        return RECEIPT_STALE
    expires = receipt.get("expires_at")
    checked_at = receipt.get("checked_at")
    if (type(expires) not in (int, float) or type(checked_at) not in (int, float)
            or not checked_at <= checked < expires or expires - checked_at > 24 * 3600):
        return RECEIPT_STALE
    if receipt.get("outcome") != "passed":
        return str(receipt.get("reason_code") or "near_tee_catalog_check_failed")
    return None


FEATURE_BLOCK_REASONS = {"tools": "near_tee_model_tools_unsupported"}


def catalog_receipt_feature_block_reason(slot_name: str, feature: str, *,
                                         receipts: dict[str, dict[str, object]] | None = None) -> str | None:
    """A caller needing ``feature`` (e.g. ``tools``) requires it in the receipt's
    catalog ``supportedFeatures``; run after ``catalog_receipt_block_reason``
    has accepted the receipt. Fails closed with a stable reason."""
    receipt = (read_catalog_receipts() if receipts is None else receipts).get(slot_name) or {}
    features = receipt.get("features")
    if receipt.get("outcome") != "passed" or not isinstance(features, list) or feature not in features:
        return FEATURE_BLOCK_REASONS.get(feature, "near_tee_model_feature_unsupported")
    return None


def tee_hosting_label(slot_name: str, profile, *, revision: int | None = None,
                      now: float | None = None) -> str | None:
    """The only TEE statement: present only while the receipt is current."""
    receipts = read_catalog_receipts()
    if catalog_receipt_block_reason(slot_name, profile, now=now, receipts=receipts, revision=revision) is not None:
        return None
    return TEE_LABEL_TEMPLATE.format(as_of=receipts[slot_name].get("checked_at_iso"))


async def fetch_catalog_record(model_id: str) -> bytes:
    from .near_client import near_http_client, read_near_response
    async with near_http_client() as client:
        raw, _headers = await read_near_response(client, "/model/" + quote(model_id, safe=""), body=None,
            key=None, timeout=CATALOG_TIMEOUT_SECONDS, cap=CATALOG_RESPONSE_CAP, no_aliasing=True,
            error=NearTeeCatalogError, prefix="near_tee_catalog", method="GET")
    return raw


async def run_catalog_check(configuration, *, now: float | None = None) -> dict[str, dict[str, object]]:
    """Check every enabled implemented slot; returns the new receipts (not yet stored)."""
    from .configuration import NEAR_TEE_IMPLEMENTED_SLOTS, near_tee_profile_for_slot
    setup = configuration.near_tee
    receipts: dict[str, dict[str, object]] = {}
    for slot_name in NEAR_TEE_IMPLEMENTED_SLOTS:
        slot = (setup.slots or {}).get(slot_name) if setup is not None else None
        if slot is None or not slot.enabled:
            continue
        profile = near_tee_profile_for_slot(slot_name, slot)
        raw = facts = required = None
        try:
            raw = await fetch_catalog_record(slot.model_id)
            facts = parse_catalog_record(raw, model_id=slot.model_id)
            reason, required = evaluate_catalog(facts, slot_name=slot_name, slot=slot)
        except NearTeeCatalogError as exc:
            reason = exc.code
        receipts[slot_name] = build_receipt(slot_name=slot_name, slot=slot, profile=profile, facts=facts,
            raw=raw, reason=reason, required=required, revision=configuration.egress_revision,
            now=time.time() if now is None else now)
    return receipts
