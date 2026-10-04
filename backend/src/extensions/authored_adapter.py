"""Closed, data-only adapter contracts. Never import or execute package Python."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

PROFILE = "json-python-bwrap-v1"
DESCRIPTOR_PATH = "adapters/adapter.json"
LIMITS = {"cpu_seconds": 2, "address_space_bytes": 134217728, "wall_seconds": 10,
          "processes": 1, "stdout_bytes": 8192, "stderr_bytes": 8192,
          "file_descriptors": 64, "input_bytes": 32768, "output_bytes": 65536}


class AdapterContractError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def json_value(raw: bytes, *, maximum: int):
    if len(raw) > maximum:
        raise AdapterContractError("adapter_json_size_limit")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise AdapterContractError("adapter_duplicate_json_key")
            result[key] = value
        return result
    def invalid(_):
        raise AdapterContractError("adapter_nonfinite_json")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise AdapterContractError("adapter_invalid_json") from exc
    nodes = 0
    def walk(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > 32 or nodes > 4096:
            raise AdapterContractError("adapter_json_structure_limit")
        if type(item) is dict:
            for child in item.values():
                walk(child, depth + 1)
        elif type(item) is list:
            for child in item:
                walk(child, depth + 1)
        elif type(item) not in {str, int, bool, type(None)}:
            raise AdapterContractError("adapter_json_type_unsupported")
    walk(value, 0)
    return value


def validate_schema(schema):
    if len(canonical(schema)) > 8192:
        raise AdapterContractError("adapter_schema_size_limit")
    nodes = 0
    def visit(node, depth):
        nonlocal nodes
        nodes += 1
        if depth > 8 or nodes > 128 or type(node) is not dict:
            raise AdapterContractError("adapter_schema_structure_limit")
        kind = node.get("type")
        keys = {"type", "const"}
        if kind == "object":
            keys |= {"properties", "required", "additionalProperties"}
            properties, required = node.get("properties"), node.get("required")
            if (type(properties) is not dict or len(properties) > 32 or
                type(required) is not list or any(type(key) is not str for key in required) or
                len(set(required)) != len(required) or
                any(type(key) is not str or not key or len(key.encode()) > 128 for key in properties) or
                any(type(key) is not str or key not in properties for key in required) or
                node.get("additionalProperties") is not False):
                raise AdapterContractError("adapter_schema_closed_object_required")
            for child in properties.values():
                visit(child, depth + 1)
        elif kind == "array":
            keys |= {"items", "minItems", "maxItems"}
            bounds(node, "minItems", "maxItems", 4096)
            visit(node.get("items"), depth + 1)
        elif kind == "string":
            keys |= {"minLength", "maxLength", "enum"}
            bounds(node, "minLength", "maxLength", 65536)
            if "enum" in node:
                values = node["enum"]
                if (type(values) is not list or not 1 <= len(values) <= 32 or
                    any(type(v) is not str for v in values) or len(set(values)) != len(values) or
                    any(not node["minLength"] <= len(v.encode()) <= node["maxLength"] for v in values)):
                    raise AdapterContractError("adapter_schema_enum_invalid")
        elif kind == "integer":
            keys |= {"minimum", "maximum"}
            if (type(node.get("minimum")) is not int or type(node.get("maximum")) is not int or
                not -(2**63) <= node["minimum"] <= node["maximum"] < 2**63):
                raise AdapterContractError("adapter_schema_integer_bounds_required")
        elif kind not in {"boolean", "null"}:
            raise AdapterContractError("adapter_schema_type_unsupported")
        if set(node) - keys:
            raise AdapterContractError("adapter_schema_unknown_keyword")
        if "const" in node:
            validate_value(node, node["const"])
    visit(schema, 0)
    return schema


def bounds(node, lower, upper, maximum):
    if (type(node.get(lower)) is not int or type(node.get(upper)) is not int or
        not 0 <= node[lower] <= node[upper] <= maximum):
        raise AdapterContractError("adapter_schema_finite_bounds_required")


def validate_value(schema, value):
    kind = schema["type"]
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "boolean": bool, "null": type(None)}
    if type(value) is not types[kind]:
        raise AdapterContractError("adapter_value_type_mismatch")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise AdapterContractError("adapter_value_const_mismatch")
    if kind == "object":
        if set(value) - set(schema["properties"]) or set(schema["required"]) - set(value):
            raise AdapterContractError("adapter_value_object_keys_invalid")
        for key, child in value.items():
            validate_value(schema["properties"][key], child)
    elif kind == "array":
        if not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise AdapterContractError("adapter_value_array_limit")
        for child in value:
            validate_value(schema["items"], child)
    elif kind == "string":
        if (not schema["minLength"] <= len(value.encode("utf-8")) <= schema["maxLength"] or
            "enum" in schema and value not in schema["enum"]):
            raise AdapterContractError("adapter_value_string_limit")
    elif kind == "integer" and not schema["minimum"] <= value <= schema["maximum"]:
        raise AdapterContractError("adapter_value_integer_limit")
    return value


def read_member(root: Path, reference: str, maximum: int):
    parts = reference.split("/")
    if not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
        raise AdapterContractError("adapter_path_invalid")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    file_fd = -1
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        before = os.fstat(file_fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum or
            before.st_mode & 0o111):
            raise AdapterContractError("adapter_member_unsafe")
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(file_fd, min(65536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(file_fd)
        if len(raw) > maximum or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise AdapterContractError("adapter_member_changed")
        return bytes(raw)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(fd)


@dataclass(frozen=True)
class AuthoredAdapter:
    descriptor: dict
    code: bytes
    descriptor_sha256: str

    @property
    def capability_id(self):
        return self.descriptor["capability_id"]

    def input(self, raw):
        return validate_value(self.descriptor["input_schema"], json_value(raw, maximum=LIMITS["input_bytes"]))

    def output(self, raw):
        return validate_value(self.descriptor["output_schema"], json_value(raw, maximum=LIMITS["output_bytes"]))


def load_adapter(root: Path, manifest):
    """Static physical validation only; returned code is never evaluated here."""
    raw = read_member(root, DESCRIPTOR_PATH, 32768)
    descriptor = json_value(raw, maximum=32768)
    fields = {"schema_version", "adapter_id", "capability_id", "display_name", "summary",
              "capability_version", "executor", "profile", "profile_contract_version", "code_entry",
              "code_sha256", "input_schema", "output_schema", "input_schema_sha256",
              "output_schema_sha256", "resources", "output_content_type", "data_class", "no_learning"}
    if type(descriptor) is not dict or set(descriptor) != fields:
        raise AdapterContractError("adapter_descriptor_closed_schema_required")
    adapter_id = descriptor["adapter_id"]
    if type(adapter_id) is not str or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", adapter_id):
        raise AdapterContractError("adapter_id_invalid")
    expected = f"pack.{manifest.id}.{adapter_id}.v1"
    fixed = {"schema_version": 1, "capability_version": "1", "capability_id": expected,
             "executor": "isolated-json-v1", "profile": PROFILE, "profile_contract_version": 1,
             "code_entry": "adapter.py", "resources": LIMITS,
             "output_content_type": "application/json", "data_class": "internal", "no_learning": True}
    if any(type(descriptor[key]) is not type(value) or descriptor[key] != value for key, value in fixed.items()):
        raise AdapterContractError("adapter_fixed_contract_mismatch")
    if any(type(value) is not int for value in descriptor["resources"].values()):
        raise AdapterContractError("adapter_resource_integer_required")
    for key, maximum in (("display_name", 128), ("summary", 512)):
        if type(descriptor[key]) is not str or not 1 <= len(descriptor[key].encode()) <= maximum:
            raise AdapterContractError("adapter_display_text_invalid")
    if manifest.contributes.adapters != [DESCRIPTOR_PATH] or manifest.contributes.capabilities != [expected]:
        raise AdapterContractError("adapter_capability_identity_mismatch")
    if (manifest.dependencies or manifest.authority.tools != ["isolated_json_adapter"] or
        manifest.authority.filesystem != ["workspace_read", "workspace_write"] or manifest.authority.network or
        manifest.authority.secrets or manifest.authority.approval != "always" or
        manifest.resources.max_runtime_seconds != 10 or manifest.resources.max_artifact_bytes != 65536 or
        manifest.resources.max_inference_cost_microusd != 0 or manifest.resources.inference_priority.value != "interactive_chat" or
        manifest.data_policy.classes != ["internal"] or manifest.data_policy.egress or manifest.policy_overlays):
        raise AdapterContractError("adapter_authority_not_closed")
    for direction in ("input", "output"):
        schema = validate_schema(descriptor[f"{direction}_schema"])
        if descriptor[f"{direction}_schema_sha256"] != sha256(canonical(schema)):
            raise AdapterContractError("adapter_schema_digest_mismatch")
    code = read_member(root, "adapter.py", 32768)
    try:
        code.decode("utf-8")
    except UnicodeError as exc:
        raise AdapterContractError("adapter_code_utf8_required") from exc
    if not code or descriptor["code_sha256"] != sha256(code):
        raise AdapterContractError("adapter_code_digest_mismatch")
    files, total = 0, 0
    for member in root.rglob("*"):
        metadata = member.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_mode & 0o111:
            raise AdapterContractError("adapter_package_member_unsafe")
        files += 1
        total += metadata.st_size
        if files > 32 or total > 524288:
            raise AdapterContractError("adapter_package_limit")
    if len(manifest.contributes.evals) > 3:
        raise AdapterContractError("adapter_eval_limit")
    for reference in manifest.contributes.evals:
        vector = json_value(read_member(root, reference, 98304), maximum=98304)
        if type(vector) is not dict or set(vector) != {"input", "output"}:
            raise AdapterContractError("adapter_eval_closed_schema_required")
        validate_value(descriptor["input_schema"], vector["input"])
        validate_value(descriptor["output_schema"], vector["output"])
    return AuthoredAdapter(descriptor, code, sha256(raw))
