#!/usr/bin/env python3
"""Emit the redacted, keyless health receipt for Epic #736.

This command is deliberately a release-gate collector rather than a provider
canary.  It performs configuration and source-contract checks and records live
provider checks as ``skipped`` until an operator runs a separately authorised
canary.  In particular, it never opens a socket to OpenRouter, a GPU host, a
VLM wrapper, or a tool connector.

The managed entry point is ``./manage.sh -e prod health --format json``.  The
receipt contains logical artifact references only; host paths, credentials,
prompts, screenshots, audio, and response bodies are never copied into it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
VALID_STATUSES = {"pass", "degraded", "blocked", "skipped", "unknown", "failed"}
OPTIONAL_STATUSES = {"degraded", "skipped"}
EXIT_PASS = 0
EXIT_OPTIONAL_DEGRADED = 2
EXIT_REQUIRED_BLOCKED = 3
EXIT_FAILED = 4
EXIT_USAGE = 64
EXIT_INTERNAL = 70


@dataclass(frozen=True)
class Criterion:
    identifier: str
    owner_issue: int
    required: bool
    summary: str
    recovery: str
    paths: tuple[str, ...] = ()


# Keep this list in sync with scripts/epic_736_health_matrix.yaml.  The Python
# copy avoids a PyYAML runtime dependency in the production image.
CRITERIA: tuple[Criterion, ...] = (
    Criterion("core.authenticated_origin", 747, True, "Authentication and origin boundary contract is present.", "Restore the authenticated ingress contract before enabling production traffic.", ("backend/src/auth/middleware.py", "backend/tests/test_operator_auth.py")),
    Criterion("core.frontend", 746, True, "Operator cockpit source contract is present.", "Restore the cockpit build and its operator status binding.", ("frontend/src",)),
    Criterion("core.api_ws", 746, True, "HTTP and WebSocket application entry points are present.", "Restore the application and WebSocket routes before accepting turns.", ("backend/src/app.py", "backend/src/api/ws.py")),
    Criterion("core.db_workspace", 742, True, "Canonical workspace and database contracts are present.", "Repair the canonical workspace mount and database preflight.", ("backend/src/workspace/production.py", "backend/src/db/engine.py")),
    Criterion("runtime.openrouter_model_fabric", 741, True, "OpenRouter model-fabric policy and configuration contracts are present.", "Repair the provider policy and persisted setup before enabling inference.", ("backend/src/model_fabric", "backend/src/llm_runtime.py")),
    Criterion("runtime.remote_inference_admission", 744, True, "Remote inference admission contract is present.", "Restore admission, budget, and cancellation enforcement before inference.", ("backend/src/model_fabric/remote_inference_admission.py", "backend/src/model_fabric/execution.py")),
    Criterion("runtime.effect_reconciliation", 743, True, "Durable effect and reconciliation contracts are present.", "Repair durable effect reconciliation before retrying external work.", ("backend/src/workflows/durable_state.py", "backend/src/workflows/production_workflow_guarantees.py")),
    Criterion("runtime.openrouter_text_receipt", 741, False, "Live OpenRouter text canary was not run by this keyless command.", "Run the separately authorised provider canary with a supplied key and retain its redacted receipt.", ()),
    Criterion("runtime.openrouter_multimodal_receipt", 751, False, "Live OpenRouter multimodal canary was not run by this keyless command.", "Run isolated audio and vision canaries when provider credentials and test media are authorised.", ()),
    Criterion("runtime.no_local_inference_dependency", 775, True, "Production configuration has no local model, GPU, VLM, Whisper, or Piper route.", "Clear local inference variables and remove any hidden fallback before deployment.", ()),
    Criterion("scheduler.priority_queue", 744, True, "Bounded scheduler and admission source contracts are present.", "Restore priority, serial admission, cancellation, and recovery handling.", ("backend/src/scheduler", "backend/src/model_fabric/remote_inference_admission.py")),
    Criterion("security.capability_caller_enforcement", 747, True, "Capability caller and egress enforcement contracts are present.", "Restore caller authorization and fail-closed egress checks.", ("backend/src/native_tools", "backend/src/security")),
    Criterion("guardian.goal_revision_stop", 745, True, "Goal revision and stale-plan stop contracts are present.", "Repair goal snapshot and stale-plan checks before proactive execution.", ("backend/src/guardian", "backend/src/goals")),
    Criterion("guardian.verified_progress", 746, True, "Guardian outcome and verification contracts are present.", "Restore outcome verification and operator-visible degraded states.", ("backend/src/guardian", "backend/src/api/operator.py")),
    Criterion("memory.embedding_capability", 775, False, "Live embedding receipt was not run by this keyless command.", "Run the separately authorised embedding canary and record model, version, dimension, and rollback evidence.", ("backend/src/memory/embedder.py", "backend/src/memory")),
    Criterion("memory.correction_delete_restore", 753, True, "Memory correction, deletion, and restore contracts are present.", "Restore canonical-memory correction and deletion guards before learning.", ("backend/src/memory", "backend/tests/test_memory_control.py")),
    Criterion("evolution.staged_candidate", 771, True, "Evolution candidates remain staged and reviewable.", "Keep candidate assets staged until human review and explicit promotion.", ("backend/src/evolution/runtime.py", "backend/src/api/evolution.py")),
    Criterion("evolution.hidden_evaluation", 771, True, "Evolution evaluation and hidden-split contracts are present.", "Restore held-out evaluation and version binding before evaluating a candidate.", ("backend/src/evolution/runtime.py", "backend/src/evolution/benchmark.py")),
    Criterion("evolution.scoped_canary_rollback", 771, True, "Scoped canary and rollback contracts are present.", "Restore approval-bound canary rollback and baseline recovery.", ("backend/src/evolution/runtime.py",)),
    Criterion("evaluation.comparator_scope_coverage", 754, True, "Comparator scope and evidence-boundary contracts are present.", "Refresh frozen-source comparator evidence and retain unknown coverage explicitly.", ("docs/implementation/09-benchmark-status.md", "docs/research")),
    Criterion("backup.restore", 742, True, "Workspace backup and restore contracts are present.", "Repair backup verification and restore recovery before production use.", ("backend/src/workspace", "backend/tests/test_workspace_lifecycle.py")),
    Criterion("edge.mac", 746, False, "Mac edge live readiness was not probed by this keyless command.", "Run the operator-shell edge probe in the target deployment and retain its redacted receipt.", ("backend/src/edge",)),
    Criterion("voice.audio", 751, False, "Voice and audio provider readiness was not probed by this keyless command.", "Run an isolated provider canary with test media and explicit consent.", ("backend/src/guardian/audio_ingress.py", "backend/src/guardian/multimodal_voice.py")),
    Criterion("telegram", 752, False, "Telegram delivery readiness was not probed by this keyless command.", "Run an isolated paired-channel probe with a test account and revocation receipt.", ("backend/src/extensions/telegram_ingress.py",)),
    Criterion("storage.disk", 742, True, "Workspace storage contract is available for a read-only check.", "Restore writable canonical storage and retention limits.", ("backend/src/workspace/production.py",)),
    Criterion("docs.truth", 754, True, "Canonical implementation and research documentation surfaces are present.", "Reconcile shipped, partial, blocked, and research claims before release.", ("docs/implementation/STATUS.md", "docs/implementation/12-current-app-guide.md", "docs/implementation/08-docs-contract.md")),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = result.stdout.strip()
    return commit if len(commit) == 40 and all(ch in "0123456789abcdef" for ch in commit.lower()) else "unknown"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _nonempty_env(names: Iterable[str]) -> list[str]:
    return [name for name in names if os.environ.get(name, "").strip()]


def _check(identifier: str, owner: int, status: str, summary: str, recovery: str, *, required: bool, artifact_refs: Iterable[str] = ()) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid health status for {identifier}")
    return {
        "id": identifier,
        "owner_issue": owner,
        "required": required,
        "status": status,
        "summary": summary,
        "artifact_refs": list(artifact_refs),
        "recovery": recovery,
    }


def _openrouter_config_check() -> dict[str, Any]:
    base = os.environ.get("LLM_API_BASE", "").strip().rstrip("/")
    model = os.environ.get("DEFAULT_MODEL", "").strip()
    allowed = {item.strip() for item in os.environ.get("OPENROUTER_ALLOWED_UPSTREAMS", "").split(",") if item.strip()}
    try:
        temperature = float(os.environ.get("MODEL_TEMPERATURE", ""))
        max_tokens = int(os.environ.get("MODEL_MAX_TOKENS", ""))
    except ValueError:
        temperature, max_tokens = -1.0, -1
    expected = (
        base == "https://openrouter.ai/api/v1"
        and model == "openrouter/z-ai/glm-5.3-flash"
        and _truthy(os.environ.get("OPENROUTER_PROVIDER_ONLY"))
        and not _truthy(os.environ.get("OPENROUTER_ALLOW_FALLBACKS"))
        and _truthy(os.environ.get("OPENROUTER_REQUIRE_PARAMETERS"))
        and os.environ.get("OPENROUTER_DATA_COLLECTION", "").strip().lower() == "deny"
        and "z-ai" in allowed
        and 0.0 <= temperature <= 2.0
        and 1 <= max_tokens <= 32768
    )
    return _check(
        "runtime.openrouter_model_fabric",
        741,
        "pass" if expected else "failed",
        "OpenRouter-only model, upstream, fallback, consent, and request bounds are configured." if expected else "OpenRouter model-fabric configuration is incomplete or unsafe.",
        "Set the governed OpenRouter base, model, upstream allowlist, no-fallback policy, consent policy, temperature, and token bound.",
        required=True,
        artifact_refs=("config:openrouter-policy",),
    )


def _no_local_inference_check() -> dict[str, Any]:
    names = (
        "LOCAL_MODEL", "LOCAL_LLM_API_BASE", "LOCAL_INFERENCE_URL", "OLLAMA_BASE_URL",
        "LM_STUDIO_BASE_URL", "SERAPH_VLM_MODE", "SERAPH_VLM_BASE_URL", "SERAPH_VLM_BACKEND_URL",
        "WHISPER_MODEL", "WHISPER_BASE_URL", "PIPER_MODEL", "PIPER_BASE_URL",
    )
    configured = _nonempty_env(names)
    profile_text = " ".join(
        os.environ.get(name, "")
        for name in ("RUNTIME_PROFILE_PREFERENCES", "RUNTIME_MODEL_OVERRIDES", "RUNTIME_FALLBACK_OVERRIDES", "FALLBACK_MODEL")
    ).lower()
    if os.environ.get("RUNTIME_FALLBACK_OVERRIDES", "").strip() or os.environ.get("FALLBACK_MODEL", "").strip():
        configured.append("runtime-fallback-route")
    if any(token in profile_text for token in ("local", "ollama", "gpu", "whisper", "piper", "vlm")):
        configured.append("runtime-profile-local-route")
    status = "failed" if configured else "pass"
    return _check(
        "runtime.no_local_inference_dependency",
        775,
        status,
        "No local model, GPU/VLM wrapper, Whisper, Piper, or hidden fallback route is configured." if not configured else "A local inference or speech route is configured in the production environment.",
        "Clear local inference variables and keep OpenRouter as the sole model route." if configured else "A later deployment probe may verify process absence; this keyless check makes no network call.",
        required=True,
        artifact_refs=("config:local-inference-absence",),
    )


def _contract_check(criterion: Criterion) -> dict[str, Any]:
    missing = [path for path in criterion.paths if not (ROOT / path).exists()]
    if missing:
        return _check(
            criterion.identifier,
            criterion.owner_issue,
            "blocked" if criterion.required else "skipped",
            "Required implementation contract is unavailable in this revision." if criterion.required else "Optional implementation surface is unavailable in this revision; no live probe was attempted.",
            criterion.recovery,
            required=criterion.required,
        )
    return _check(
        criterion.identifier,
        criterion.owner_issue,
        "pass",
        criterion.summary + " Source presence is checked; live execution is not claimed by this keyless receipt.",
        criterion.recovery,
        required=criterion.required,
        artifact_refs=("source-contract:" + criterion.identifier,),
    )


def _optional_live_check(criterion: Criterion) -> dict[str, Any]:
    return _check(
        criterion.identifier,
        criterion.owner_issue,
        "skipped",
        criterion.summary + " No provider, connector, edge, or media call was made.",
        criterion.recovery,
        required=False,
    )


def _overall(checks: list[dict[str, Any]]) -> tuple[str, int]:
    statuses = {str(item["status"]) for item in checks}
    if "failed" in statuses:
        return "failed", EXIT_FAILED
    if any(item["required"] and item["status"] in {"blocked", "unknown"} for item in checks):
        return "blocked", EXIT_REQUIRED_BLOCKED
    if any(item["required"] and item["status"] == "degraded" for item in checks):
        return "degraded", EXIT_REQUIRED_BLOCKED
    if any(item["status"] in OPTIONAL_STATUSES or item["status"] == "unknown" for item in checks):
        return "degraded", EXIT_OPTIONAL_DEGRADED
    return "pass", EXIT_PASS


def _workspace_root() -> Path:
    configured = os.environ.get("WORKSPACE_DIR", "").strip() or os.environ.get("BACKEND_DATA_PATH_PROD", "").strip()
    if not configured:
        raise RuntimeError("canonical workspace is not configured")
    root = Path(configured).expanduser()
    if not root.is_absolute():
        raise RuntimeError("canonical workspace must be absolute")
    return root


def _write_receipt(receipt: dict[str, Any], generated_at: datetime) -> str:
    directory = _workspace_root() / "operator-receipts" / "epic-736-health"
    directory.mkdir(parents=True, exist_ok=True)
    filename = generated_at.strftime("%Y%m%dT%H%M%SZ") + ".json"
    target = directory / filename
    payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile("wb", dir=directory, prefix=".health-", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return f"operator-receipts/epic-736-health/{filename}"


def build_receipt() -> tuple[dict[str, Any], int, str]:
    generated = _utc_now()
    checks: list[dict[str, Any]] = []
    # The model-fabric entry is special because it is also the configuration
    # authority consumed by the rest of the checks.
    checks.append(_openrouter_config_check())
    checks.append(_no_local_inference_check())
    for criterion in CRITERIA:
        if criterion.identifier in {"runtime.openrouter_model_fabric", "runtime.no_local_inference_dependency"}:
            continue
        if not criterion.required and not criterion.paths:
            checks.append(_optional_live_check(criterion))
        else:
            checks.append(_contract_check(criterion))

    overall_status, exit_code = _overall(checks)
    skipped = [
        {"id": item["id"], "reason": item["summary"], "recovery": item["recovery"]}
        for item in checks
        if item["status"] in {"skipped", "unknown"}
    ]
    residual_risks = [
        "Live provider, embedding, edge, voice, and Telegram evidence remains unprobed by the keyless command; no superiority or production-readiness claim follows.",
        "Source-contract presence does not replace focused tests, runtime/UI receipts, or isolated mutating drills.",
    ]
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "epic": 736,
        "generated_at": _timestamp(generated),
        "environment": "prod",
        "commit": _safe_commit(),
        "overall_status": overall_status,
        "checks": checks,
        "redactions": {"count": 0, "classes": ["credentials", "message_content", "raw_media", "sensitive_paths"]},
        "skipped": skipped,
        "residual_risks": residual_risks,
    }
    logical_path = _write_receipt(receipt, generated)
    return receipt, exit_code, logical_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "text"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        # argparse's documented usage errors are stable and must not be
        # confused with a health failure.
        return EXIT_PASS if int(exc.code) == 0 else EXIT_USAGE
    try:
        receipt, exit_code, logical_path = build_receipt()
    except RuntimeError:
        print("health receipt generation failed: canonical workspace configuration error", file=sys.stderr)
        return EXIT_USAGE
    except (OSError, ValueError):
        print("health receipt generation failed: canonical workspace or schema error", file=sys.stderr)
        return EXIT_INTERNAL
    if args.format == "json":
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"Epic #736 health: {receipt['overall_status']} (exit {exit_code})")
        for item in receipt["checks"]:
            print(f"{item['status']:>8} {item['id']}: {item['summary']}")
    print(f"receipt: {logical_path}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
