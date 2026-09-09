"""CPU-host production preflight and readiness receipt.

This check deliberately validates only local core configuration.  OpenRouter
is the single active inference route, but preflight never calls it and never
probes a local model, CUDA device, or VLM wrapper.  A missing provider key or
policy is therefore reported as inference configuration state while the
canonical workspace and authenticated core can still start.

The module is dependency-free so the container can run it before importing the
application.  ``main`` returns a non-zero status only when production core
startup cannot be safe (for example, missing operator authentication or an
unusable workspace); inference configuration remains a separate receipt.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import base64
import binascii
import json
import os
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from src.workspace.production import (
    ProductionWorkspaceMountError,
    validate_container_workspace_mount,
)


SCHEMA = "seraph.cpu-host-preflight.v1"
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
DEFAULT_WORKSPACE = "/app/data"
PLACEHOLDER_SECRETS = {
    "your-prod-openrouter-key",
    "your-openrouter-key",
    "your-key-here",
    "replace-me",
    "replace_with_secret",
}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "") or "").strip().strip("'\"")


def _secret_configured(value: str) -> bool:
    return bool(value) and value.lower() not in PLACEHOLDER_SECRETS and not value.lower().startswith("your-")


def _pbkdf2_hash_shape_valid(value: str) -> bool:
    """Match the exact encoded shape consumed by ``src.auth.service``."""
    if not value or not _secret_configured(value):
        return False
    parts = value.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256" or parts[1] != "600000":
        return False
    try:
        salt = base64.b64decode(parts[2].encode("ascii"), altchars=b"-_", validate=True)
        digest = base64.b64decode(parts[3].encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError, binascii.Error):
        return False
    return len(salt) == 16 and len(digest) == 32


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool(value: str, *, default: bool) -> bool:
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _auth_check(env: Mapping[str, str], *, production: bool) -> Check:
    raw_secret = _secret_configured(_value(env, "OPERATOR_AUTH_SECRET"))
    encoded_hash = _value(env, "OPERATOR_AUTH_SECRET_HASH")
    hash_present = _secret_configured(encoded_hash)
    if hash_present and not _pbkdf2_hash_shape_valid(encoded_hash):
        return Check("operator_auth", "invalid", "operator PBKDF2 hash has invalid shape")
    hashed_secret = _pbkdf2_hash_shape_valid(encoded_hash)
    configured = int(raw_secret) + int(hashed_secret)
    if not production:
        return Check("operator_auth", "not_required", "development/test auth policy owns this environment")
    if configured == 1:
        return Check("operator_auth", "ready", "one server-side operator credential is configured")
    if configured == 0:
        return Check("operator_auth", "configuration_required", "configure exactly one operator secret or PBKDF2 hash")
    return Check("operator_auth", "invalid", "configure exactly one operator secret or PBKDF2 hash")


def _workspace_check(env: Mapping[str, str]) -> Check:
    raw_path = _value(env, "WORKSPACE_DIR") or DEFAULT_WORKSPACE
    path = Path(raw_path).expanduser()
    try:
        if path.exists():
            if not path.is_dir():
                return Check("canonical_workspace", "invalid", f"workspace path is not a directory: {path}")
            if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                return Check("canonical_workspace", "unavailable", f"workspace is not readable and writable: {path}")
            return Check("canonical_workspace", "ready", f"workspace is mounted and writable: {path}")
        parent = path.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if parent.exists() and os.access(parent, os.R_OK | os.W_OK | os.X_OK):
            return Check("canonical_workspace", "ready", f"workspace will be created below writable parent: {path}")
        return Check("canonical_workspace", "unavailable", f"workspace parent is not writable: {parent}")
    except OSError as exc:
        return Check("canonical_workspace", "unavailable", f"workspace check failed: {exc}")


def _production_mount_check(env: Mapping[str, str]) -> Check:
    """Verify the Compose backend sees the one canonical container mount."""
    try:
        validate_container_workspace_mount(env)
    except ProductionWorkspaceMountError as exc:
        return Check("canonical_workspace_mount", "invalid", exc.reason_code)
    return Check(
        "canonical_workspace_mount",
        "ready",
        "production workspace is mounted at /app/data",
    )


def _inference_receipt(env: Mapping[str, str]) -> dict[str, object]:
    reasons: list[str] = []
    api_base = _value(env, "LLM_API_BASE") or OPENROUTER_API_BASE
    if api_base.rstrip("/") != OPENROUTER_API_BASE:
        reasons.append("openrouter_api_base_not_canonical")
    try:
        parsed = urlsplit(api_base)
        if parsed.scheme != "https" or parsed.netloc != "openrouter.ai":
            reasons.append("openrouter_route_not_https")
    except ValueError:
        reasons.append("openrouter_api_base_invalid")

    model = _value(env, "DEFAULT_MODEL")
    if not model:
        model = "unknown"
        reasons.append("openrouter_model_missing")
    elif not model.startswith("openrouter/"):
        reasons.append("openrouter_model_not_selected")
    if not _secret_configured(_value(env, "OPENROUTER_API_KEY")):
        reasons.append("openrouter_api_key_missing")
    if not _csv(_value(env, "OPENROUTER_ALLOWED_UPSTREAMS")):
        reasons.append("openrouter_upstream_allowlist_missing")
    if not _bool(_value(env, "OPENROUTER_PROVIDER_ONLY"), default=True):
        reasons.append("openrouter_only_mode_disabled")
    if _bool(_value(env, "OPENROUTER_ALLOW_FALLBACKS"), default=False):
        reasons.append("openrouter_fallbacks_enabled")
    if not _bool(_value(env, "OPENROUTER_REQUIRE_PARAMETERS"), default=True):
        reasons.append("openrouter_parameter_requirement_disabled")
    if (_value(env, "OPENROUTER_DATA_COLLECTION") or "deny").lower() != "deny":
        reasons.append("openrouter_data_policy_not_deny")
    if _value(env, "FALLBACK_MODEL") or _value(env, "FALLBACK_MODELS") or _value(env, "FALLBACK_LLM_API_BASE"):
        reasons.append("fallback_route_configured")

    deduped_reasons = list(dict.fromkeys(reasons))
    # This offline check cannot establish a live provider receipt.  Keep the
    # readiness status gated until the authenticated runtime has its own
    # provider proof, even when the static environment is complete.
    static_status = "configuration_required" if deduped_reasons else "configured"
    return {
        "provider": "openrouter",
        "route": OPENROUTER_API_BASE,
        "model": model,
        "status": "configuration_required",
        "configuration_status": static_status,
        "reasons": deduped_reasons,
        "live_proof": "unknown",
        "probe_performed": False,
        "local_fallback": "disabled",
    }


def build_preflight_report(env: Mapping[str, str] | None = None) -> dict[str, object]:
    """Build a local-only CPU-host readiness receipt.

    ``env`` is injectable for deterministic tests.  No function in this module
    opens a socket, inspects a GPU, or contacts a provider.
    """

    values = env if env is not None else os.environ
    production = _value(values, "DEPLOYMENT_ENVIRONMENT").lower() in {"prod", "production"}
    checks = [_auth_check(values, production=production), _workspace_check(values)]
    if production:
        if _bool(_value(values, "SERAPH_PRODUCTION_MOUNT_CHECK"), default=False):
            checks.append(_production_mount_check(values))
        else:
            # The documented host invocation cannot inspect a container's
            # /proc/self/mountinfo. Keep that limitation explicit instead of
            # silently presenting a host-static receipt as mount proof. The
            # production Compose command sets this flag and fails closed in
            # the container before the backend starts.
            checks.append(
                Check(
                    "canonical_workspace_mount",
                    "deferred",
                    "container startup performs the /app/data bind identity check",
                )
            )
    core_status = "ready"
    if any(check.status == "invalid" for check in checks):
        core_status = "invalid"
    elif any(check.status == "unavailable" for check in checks):
        core_status = "unavailable"
    elif any(check.status == "configuration_required" for check in checks):
        core_status = "configuration_required"

    inference = _inference_receipt(values)
    return {
        "schema": SCHEMA,
        "host_profile": "cpu",
        "core": {
            "status": core_status,
            "checks": [asdict(check) for check in checks],
            "health_route": "/health",
            "canonical_state": "local_workspace",
        },
        "inference": inference,
        "dependencies": {
            "cuda": "not_required",
            "gpu_device": "not_required",
            "model_weights": "not_required",
            "local_model_server": "not_required",
            "vlm_wrapper": "not_required",
        },
        "private_listener_contract": {
            "status": "managed_configuration",
            "local_frontend_bind": "127.0.0.1",
            "local_backend_bind": "127.0.0.1",
            "container_backend_host_publication": "disabled",
        },
        "overall_status": "ready" if core_status == "ready" else core_status,
    }


def _format_text(report: Mapping[str, object]) -> str:
    core = report["core"]
    inference = report["inference"]
    assert isinstance(core, Mapping)
    assert isinstance(inference, Mapping)
    reasons = ", ".join(str(item) for item in inference.get("reasons") or ()) or "none"
    return "\n".join(
        (
            f"CPU-host core: {core.get('status')}",
            f"OpenRouter inference: {inference.get('status')} (live proof: {inference.get('live_proof')})",
            f"Inference reasons: {reasons}",
            "GPU/CUDA/model weights/local model server/VLM wrapper: not required",
        )
    )


def _read_env_file(path: Path) -> dict[str, str]:
    """Read the small ``KEY=value`` subset used by the deployment examples."""

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid env file line {line_number}: expected KEY=value")
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
            raise ValueError(f"invalid env file key on line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="read deployment values from a KEY=value file before evaluating readiness",
    )
    args = parser.parse_args(argv)
    values = dict(os.environ)
    if args.env_file is not None:
        values.update(_read_env_file(args.env_file))
    report = build_preflight_report(values)
    if args.format == "json":
        print(json.dumps(report, sort_keys=True))
    else:
        print(_format_text(report))
    return 0 if report["overall_status"] == "ready" else 78


if __name__ == "__main__":
    raise SystemExit(main())
