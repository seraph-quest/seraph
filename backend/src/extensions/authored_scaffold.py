"""Static starter for a reviewed isolated time-ledger capability package."""
from pathlib import Path
import json
import os
import re
import yaml

from src.extensions.authored_adapter import LIMITS, PROFILE, canonical, sha256

CODE = b'''import json
with open("/input.json", "r", encoding="utf-8") as source:
    request = json.load(source)
groups = {}
for row in request["rows"]:
    category = row["category"]
    group = groups.setdefault(category, {"category": category, "count": 0, "total_minutes": 0})
    group["count"] += 1
    group["total_minutes"] += row["minutes"]
result = {"schema_version": 1, "groups": [groups[key] for key in sorted(groups)],
          "total_minutes": sum(group["total_minutes"] for group in groups.values())}
with open("/out/result.json", "w", encoding="utf-8") as destination:
    json.dump(result, destination, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    destination.write("\\n")
'''


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def schemas():
    version = {"type": "integer", "minimum": 1, "maximum": 1, "const": 1}
    category = {"type": "string", "minLength": 1, "maxLength": 64}
    integer = lambda maximum, minimum=0: {"type": "integer", "minimum": minimum, "maximum": maximum}
    array = lambda items: {"type": "array", "items": items, "minItems": 0, "maxItems": 128}
    return (object_schema({"schema_version": version,
                           "rows": array(object_schema({"category": category, "minutes": integer(1440)}))}),
            object_schema({"schema_version": version,
                           "groups": array(object_schema({"category": category, "count": integer(128, 1),
                                                          "total_minutes": integer(184320)})),
                           "total_minutes": integer(184320)}))


def scaffold_adapter(root, *, package_id, display_name, adapter_id="summarize"):
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*", package_id) or len(package_id) > 80:
        raise ValueError("adapter_package_id_invalid")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", adapter_id):
        raise ValueError("adapter_id_invalid")
    from src.execution.tool_package_profile import package_manifest
    manifest = package_manifest().model_dump(mode="json")
    capability_id = f"pack.{package_id}.{adapter_id}.v1"
    manifest.update(id=package_id, display_name=display_name)
    manifest["publisher"] = {"name": "Local author", "provenance": "unsigned-local"}
    manifest["contributes"] = {"capabilities": [capability_id], "adapters": ["adapters/adapter.json"],
                               "evals": ["evals/known-answer.json"]}
    manifest["authority"]["tools"] = ["isolated_json_adapter"]
    input_schema, output_schema = schemas()
    descriptor = {"schema_version": 1, "adapter_id": adapter_id, "capability_id": capability_id,
                  "display_name": display_name, "summary": "Summarize bounded category/minute time-ledger rows.",
                  "capability_version": "1", "executor": "isolated-json-v1", "profile": PROFILE,
                  "profile_contract_version": 1, "code_entry": "adapter.py", "code_sha256": sha256(CODE),
                  "input_schema": input_schema, "output_schema": output_schema,
                  "input_schema_sha256": sha256(canonical(input_schema)),
                  "output_schema_sha256": sha256(canonical(output_schema)), "resources": LIMITS,
                  "output_content_type": "application/json", "data_class": "internal", "no_learning": True}
    vector = {"input": {"schema_version": 1, "rows": [{"category": "engineering", "minutes": 30},
                                                       {"category": "engineering", "minutes": 45},
                                                       {"category": "reading", "minutes": 20}]},
              "output": {"schema_version": 1, "groups": [{"category": "engineering", "count": 2, "total_minutes": 75},
                                                          {"category": "reading", "count": 1, "total_minutes": 20}],
                         "total_minutes": 95}}
    root = Path(root)
    root.mkdir(mode=0o700, parents=False)
    (root / "adapters").mkdir(mode=0o700)
    (root / "evals").mkdir(mode=0o700)
    files = {"manifest.yaml": yaml.safe_dump(manifest, sort_keys=False).encode(), "adapter.py": CODE,
             "adapters/adapter.json": json.dumps(descriptor, indent=2, ensure_ascii=False).encode() + b"\n",
             "evals/known-answer.json": json.dumps(vector, indent=2).encode() + b"\n",
             "README.md": b"# Reviewed local time-ledger summary\n\nUnsigned local code. Review exact code and contracts before approval.\nAuthor/validate commands never execute it. Golden vectors are review data.\n"}
    for reference, raw in files.items():
        fd = os.open(root / reference, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as destination:
            destination.write(raw)
    return root / "manifest.yaml"
