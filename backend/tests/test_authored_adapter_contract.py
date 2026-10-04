"""Static contracts never evaluate author-controlled source or golden vectors."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.extensions.authored_adapter import AdapterContractError, canonical, json_value, load_adapter, sha256, validate_schema, validate_value
from src.extensions.authored_scaffold import scaffold_adapter, schemas
from src.extensions.capability_pack import parse_capability_pack_manifest, validate_capability_pack_package


def package(tmp_path):
    root = tmp_path / "ledger"
    scaffold_adapter(root, package_id="local.time-ledger-summary", display_name="Time ledger summary")
    manifest = parse_capability_pack_manifest((root / "manifest.yaml").read_text())
    return root, manifest


def test_static_cli_and_validation_never_execute_code(tmp_path):
    root, manifest = package(tmp_path)
    marker = tmp_path / "host-execution"
    code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\nraise RuntimeError('must never run')\n".encode()
    (root / "adapter.py").write_bytes(code)
    descriptor = json.loads((root / "adapters/adapter.json").read_bytes())
    descriptor["code_sha256"] = sha256(code)
    (root / "adapters/adapter.json").write_bytes(canonical(descriptor))
    result = validate_capability_pack_package(root, manifest=manifest)
    assert result["ok"], result
    script = Path(__file__).resolve().parents[2] / "scripts/extensions/validate_pack.py"
    completed = subprocess.run([sys.executable, str(script), str(root)], capture_output=True, timeout=10)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not marker.exists()


def test_time_ledger_closed_contract_and_known_answer_data(tmp_path):
    root, manifest = package(tmp_path)
    adapter = load_adapter(root, manifest)
    assert adapter.capability_id == "pack.local.time-ledger-summary.summarize.v1"
    vector = json.loads((root / "evals/known-answer.json").read_bytes())
    assert adapter.input(canonical(vector["input"])) == vector["input"]
    assert adapter.output(canonical(vector["output"]))["total_minutes"] == 95
    bad = vector["input"] | {"argv": ["/bin/sh"]}
    with pytest.raises(AdapterContractError):
        adapter.input(canonical(bad))
    with pytest.raises(AdapterContractError):
        adapter.input(canonical({"schema_version": True, "rows": []}))
    with pytest.raises(AdapterContractError):
        adapter.input(canonical({"schema_version": 1, "rows": [{"category": "x", "minutes": 1441}]}))


@pytest.mark.parametrize("bad", [
    {"type": "string"}, {"type": "array", "items": {"type": "boolean"}},
    {"type": "integer", "minimum": False, "maximum": 1},
    {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
    {"type": "string", "minLength": 0, "maxLength": 10, "pattern": ".*"},
    {"$ref": "https://example.test/schema"}, {"type": "number"},
])
def test_unsupported_schema_fails_closed(bad):
    with pytest.raises(AdapterContractError):
        validate_schema(bad)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1.25}'])
def test_json_rejects_duplicates_nonfinite_and_floats(raw):
    with pytest.raises(AdapterContractError):
        json_value(raw, maximum=100)


@pytest.mark.parametrize("change", ["digest", "argv", "capability", "hardlink", "schema"])
def test_package_static_boundaries(tmp_path, change):
    root, manifest = package(tmp_path)
    path = root / "adapters/adapter.json"
    descriptor = json.loads(path.read_bytes())
    if change == "digest":
        descriptor["code_sha256"] = "0" * 64
    elif change == "argv":
        descriptor["argv"] = ["/bin/sh"]
    elif change == "capability":
        descriptor["capability_id"] = "work.json-format.v1"
    elif change == "schema":
        descriptor["input_schema"]["additionalProperties"] = True
    else:
        import os
        os.link(root / "adapter.py", root / "second.py")
    path.write_bytes(canonical(descriptor))
    result = validate_capability_pack_package(root, manifest=manifest)
    assert not result["ok"], result


def test_static_review_packet_does_not_inspect_optional_runtime(tmp_path, monkeypatch):
    from src.api.capability_packs import _authored_packet
    from src.execution import tool_package_profile
    root,manifest=package(tmp_path)
    def unexpected_runtime(_):
        raise AssertionError("Static review must not scan the optional executable runtime")
    monkeypatch.setattr(tool_package_profile,"inspect_runtime",unexpected_runtime)
    packet=_authored_packet(str(root),inspect_runtime_profile=False)
    assert packet["pack_id"]==manifest.id and packet["profile"] is None
    assert packet["descriptor"]["code_sha256"]==sha256((root/"adapter.py").read_bytes())
