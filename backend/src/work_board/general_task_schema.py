"""Conservative output-schema implication; never retrieve schema references.

This is admission proof, not a general JSON Schema subsumption engine. Exact
schemas pass the bounded contradiction guard first. Nonidentical schemas support single types, object
properties/required/additionalProperties, and finite enum/const outputs. Other
value assertions must be identical in the producing schema or fail closed.
"""
from __future__ import annotations

import json
from fractions import Fraction
from math import ceil, floor

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


def _possible(schema, depth=0, *, producer=True):
    """Reject closed, locally provable contradictions before equality proof.

    Finite values are checked against the entire schema. For nonfinite schemas
    this checks scalar bounds and required object/array members; it does not
    attempt regex satisfiability or general JSON Schema subsumption. A required
    nonfinite regex producer has unknown inhabitation and fails closed. Consumer
    patterns may still be proved by validating actual finite producer members.
    """
    if depth > 32 or schema is False:
        return False
    if schema is True:
        return True
    if "const" in schema or "enum" in schema:
        values = [schema["const"]] if "const" in schema else schema["enum"]
        return any(Draft202012Validator(schema).is_valid(value) for value in values)
    if set(schema) - (_SUPPORTED | {"items", "prefixItems", "anyOf"}):
        return False
    if "anyOf" in schema:
        # Do not infer conjunction across alternatives. A local witness must
        # satisfy the enclosing assertions as well, unless they are absent.
        if set(schema) - (_ANNOTATIONS | {"anyOf"}):
            return False
        return any(_possible(child, depth + 1, producer=producer) for child in schema["anyOf"])
    kinds = schema.get("type", ["null", "boolean", "string", "number", "object", "array"])
    if isinstance(kinds, str):
        kinds = [kinds]
    return any(_possible_type(schema, kind, depth, producer=producer) for kind in kinds)


def _possible_type(schema, kind, depth, *, producer):
    if kind in {"null", "boolean"}:
        values = [None] if kind == "null" else [False, True]
        return any(Draft202012Validator(schema).is_valid(value) for value in values)
    if kind == "string":
        if producer and "pattern" in schema:
            # The sole reviewed nonfinite regex subset has a constructive
            # witness. Check every assertion, not merely the pattern/bounds.
            # This grants no runtime result validity or caller-supplied proof.
            return (schema["pattern"] == "^[a-f0-9]{64}$"
                    and Draft202012Validator(schema).is_valid("0" * 64))
        return schema.get("minLength", 0) <= schema.get("maxLength", float("inf"))
    if kind in {"integer", "number"}:
        lower = [(Fraction(str(schema[key])), key == "exclusiveMinimum")
                 for key in ("minimum", "exclusiveMinimum") if key in schema]
        upper = [(Fraction(str(schema[key])), key == "exclusiveMaximum")
                 for key in ("maximum", "exclusiveMaximum") if key in schema]
        lo = max(lower, default=None)
        hi = min(upper, key=lambda bound: (bound[0], not bound[1]), default=None)
        if lo and hi and (lo[0] > hi[0] or (lo[0] == hi[0] and (lo[1] or hi[1]))):
            return False
        step = Fraction(str(schema.get("multipleOf", 1))) if kind == "integer" or "multipleOf" in schema else None
        if kind == "integer":
            step = Fraction(step.numerator)
        if step and lo and hi:
            first = ceil(lo[0] / step)
            if lo[1] and first * step == lo[0]:
                first += 1
            return first * step < hi[0] or (first * step == hi[0] and not hi[1])
        return True
    if kind == "object":
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        required = set(schema.get("required", []))
        minimum = max(schema.get("minProperties", 0), len(required))
        if minimum > schema.get("maxProperties", float("inf")):
            return False
        if any(not _possible(props.get(name, extra), depth + 1, producer=producer) for name in required):
            return False
        if not _possible(extra, depth + 1, producer=producer):
            return minimum <= sum(_possible(child, depth + 1, producer=producer) for child in props.values())
        return True
    if kind == "array":
        minimum = schema.get("minItems", 0)
        if minimum > schema.get("maxItems", float("inf")):
            return False
        prefix = schema.get("prefixItems", [])
        if any(not _possible(child, depth + 1, producer=producer) for child in prefix[:minimum]):
            return False
        items = schema.get("items", True)
        if minimum > len(prefix) and not _possible(items, depth + 1, producer=producer):
            return False
        if schema.get("uniqueItems") and minimum > 1:
            # Distinctness of heterogeneous prefix domains is outside this
            # proof subset. Homogeneous finite domains have a closed capacity.
            if prefix:
                return False
            capacity = _domain_capacity(items)
            if capacity is None or minimum > capacity:
                return False
        return True
    return False


def _domain_capacity(schema):
    """Exact capacity for closed finite domains; None means no proof.

    An integer lattice unbounded on either side has infinite capacity. No
    enumeration of numeric ranges, string search or caller witness is used.
    """
    if isinstance(schema, bool):
        return float("inf") if schema else 0
    values = [schema["const"]] if "const" in schema else schema.get("enum")
    if values is None and schema.get("type") in ("boolean", "null"):
        values = [False, True] if schema["type"] == "boolean" else [None]
    if values is None and schema.get("type") == "string" and schema.get("maxLength") == 0:
        values = [""]
    if values is not None:
        valid = []
        validator = Draft202012Validator(schema)
        for value in values:
            if validator.is_valid(value) and not any(
                Draft202012Validator({"const": prior}).is_valid(value) for prior in valid
            ):
                valid.append(value)
        return len(valid)
    if schema.get("type") == "integer":
        lower = [(Fraction(str(schema[key])), key == "exclusiveMinimum")
                 for key in ("minimum", "exclusiveMinimum") if key in schema]
        upper = [(Fraction(str(schema[key])), key == "exclusiveMaximum")
                 for key in ("maximum", "exclusiveMaximum") if key in schema]
        if not lower or not upper:
            return float("inf")
        lo = max(lower)
        hi = min(upper, key=lambda bound: (bound[0], not bound[1]))
        step = Fraction(str(schema.get("multipleOf", 1))).numerator
        first, last = ceil(lo[0] / step), floor(hi[0] / step)
        first += int(lo[1] and first * step == lo[0])
        last -= int(hi[1] and last * step == hi[0])
        return max(0, last - first + 1)
    return None


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
        valid = [value for value in values if producer.is_valid(value)]
        return bool(valid) and all(consumer.is_valid(value) for value in valid)
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
        if not _possible(output_schema) or not _possible(contract_schema, producer=False):
            return False
        if _canonical(output_schema) == _canonical(contract_schema):
            return True
        if not _supported(output_schema) or not _supported(contract_schema):
            return False
        return _implies(output_schema, contract_schema)
    except (SchemaError, TypeError, ValueError, RecursionError):
        return False
