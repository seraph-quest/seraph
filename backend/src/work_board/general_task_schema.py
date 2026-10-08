"""Conservative output-schema implication; never retrieve schema references.

This is admission proof, not a general JSON Schema subsumption engine. Exact
schemas are compatible. Nonidentical schemas support single types, object
properties/required/additionalProperties, and finite enum/const outputs. Other
value assertions must be identical in the producing schema or fail closed.
"""
from __future__ import annotations

import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


_ANNOTATIONS = {"title", "description", "default", "examples", "$comment"}
_ASSERTIONS = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minProperties", "maxProperties",
    "minItems", "maxItems", "uniqueItems",
}
_SUPPORTED = _ANNOTATIONS | _ASSERTIONS | {
    "type", "properties", "required", "additionalProperties", "enum", "const",
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _supported(schema, depth=0):
    if depth > 32:
        return False
    if isinstance(schema, bool):
        return True
    if not isinstance(schema, dict) or set(schema) - _SUPPORTED:
        return False
    if "type" in schema and not isinstance(schema["type"], str):
        return False
    return (all(_supported(child, depth + 1) for child in schema.get("properties", {}).values())
            and _supported(schema.get("additionalProperties", True), depth + 1))


def _implies(source, target):
    if _canonical(source) == _canonical(target) or target is True or target == {} or source is False:
        return True
    if target is False or source is True:
        return False
    source = {key: value for key, value in source.items() if key not in _ANNOTATIONS}
    target = {key: value for key, value in target.items() if key not in _ANNOTATIONS}
    if not target:
        return True
    # Finite producing sets permit a complete local proof, including value
    # constraints. Filter impossible enum members by the producing schema.
    if "const" in source or "enum" in source:
        values = [source["const"]] if "const" in source else source["enum"]
        producer, consumer = Draft202012Validator(source), Draft202012Validator(target)
        return all(consumer.is_valid(value) for value in values if producer.is_valid(value))
    if "const" in target or "enum" in target:
        return False
    source_type, target_type = source.get("type"), target.get("type")
    if target_type is not None and source_type != target_type:
        if not (source_type == "integer" and target_type == "number"):
            return False
    # We only reason about object keywords with an explicit object producer.
    if any(key in target for key in ("properties", "required", "additionalProperties")):
        if source_type != "object":
            return False
        if not set(target.get("required", [])) <= set(source.get("required", [])):
            return False
        source_props, target_props = source.get("properties", {}), target.get("properties", {})
        source_extra = source.get("additionalProperties", True)
        target_extra = target.get("additionalProperties", True)
        for name, contract in target_props.items():
            producing = source_props.get(name, source_extra)
            if not _implies(producing, contract):
                return False
        # Target additionalProperties constrains every source property that
        # the target did not explicitly name, as well as all other keys.
        for name, producing in source_props.items():
            if name not in target_props and not _implies(producing, target_extra):
                return False
        if not _implies(source_extra, target_extra):
            return False
    for key in _ASSERTIONS & target.keys():
        if key not in source or _canonical(source[key]) != _canonical(target[key]):
            return False
    return True


def schema_accepts_output(output_schema: dict, contract_schema: dict) -> bool:
    """True only when every registered output satisfies the receiving contract.

    Callers still validate schema size/reference restrictions through their
    existing task validator. This helper performs no I/O and fails closed for
    unsupported nonidentical forms; false means admission must be rejected.
    """
    try:
        Draft202012Validator.check_schema(output_schema)
        Draft202012Validator.check_schema(contract_schema)
        if _canonical(output_schema) == _canonical(contract_schema):
            return True
        if not _supported(output_schema) or not _supported(contract_schema):
            return False
        return _implies(output_schema, contract_schema)
    except (SchemaError, TypeError, ValueError, RecursionError):
        return False
