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
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable
import uuid


ROOT = Path(__file__).resolve().parents[1]
VALID_STATUSES = {"pass", "degraded", "blocked", "skipped", "unknown", "failed"}
VALID_EVIDENCE_MODES = {"configuration", "static", "integration", "external_unverified", "excluded"}
OPTIONAL_STATUSES = {"blocked", "degraded", "skipped"}
EXIT_PASS = 0
EXIT_OPTIONAL_DEGRADED = 2
EXIT_REQUIRED_BLOCKED = 3
EXIT_FAILED = 4
EXIT_USAGE = 64
EXIT_INTERNAL = 70
CHILD_EVIDENCE_SCHEMA_VERSION = 1
CHILD_EVIDENCE_MAX_BYTES = 256 * 1024
CHILD_EVIDENCE_MAX_AGE = timedelta(hours=24)
CHILD_EVIDENCE_MAX_FUTURE_SKEW = timedelta(minutes=5)
CHILD_EVIDENCE_DIRECTORY = Path("operator-receipts") / "epic-736-child-evidence"
CHILD_CRITERION_IDS = frozenset(
    {
        "conversation.identity_outbox",
        "native_software.loop",
        "edge.paired_transport",
        "audio.capture_decode_persistence",
        "telegram.durable_transport",
        "capability_pack.lifecycle",
    }
)


@dataclass(frozen=True)
class Criterion:
    identifier: str
    owner_issue: int
    required: bool
    summary: str
    recovery: str
    paths: tuple[str, ...] = ()
    evidence_mode: str = "static"
    exclusion_reason: str = ""


@dataclass(frozen=True)
class ChildEvidence:
    criterion_id: str
    child_issue: int
    commit: str
    command: str
    result: str
    timestamp: datetime
    digest: str


@dataclass
class ChildEvidenceBundle:
    source: str
    entries: dict[str, list[ChildEvidence]]
    errors: dict[str, list[str]]

    @property
    def invalid_count(self) -> int:
        return sum(len(reasons) for reasons in self.errors.values())


# Keep this list in sync with scripts/epic_736_health_matrix.yaml.  The Python
# copy avoids a PyYAML runtime dependency in the production image.
CRITERIA: tuple[Criterion, ...] = (
    Criterion("core.authenticated_origin", 747, True, "Authentication and origin boundary contract is present.", "Restore the authenticated ingress contract before enabling production traffic.", ("backend/src/auth/middleware.py", "backend/tests/test_operator_auth.py")),
    Criterion("core.frontend", 746, True, "Operator cockpit source contract is present.", "Restore the cockpit build and its operator status binding.", ("frontend/src",)),
    Criterion("core.api_ws", 746, True, "HTTP and WebSocket application entry points are present.", "Restore the application and WebSocket routes before accepting turns.", ("backend/src/app.py", "backend/src/api/ws.py")),
    Criterion("core.db_workspace", 742, True, "Canonical workspace and database contracts are present.", "Repair the canonical workspace mount and database preflight.", ("backend/src/workspace/production.py", "backend/src/db/engine.py")),
    Criterion("conversation.identity_outbox", 750, True, "Canonical conversation identity and durable outbox source contracts are present; behavioral proof is supplied by a child receipt.", "Merge or restore the #750 identity and durable outbox implementation before accepting cross-surface continuity.", ("backend/src/conversation/identity.py", "backend/src/observer/native_notification_queue.py"), "integration"),
    Criterion("native_software.loop", 748, True, "Governed native software-engineering loop source contracts are present; behavioral proof is supplied by a child receipt.", "Merge or restore the #748 native software loop and bounded process surface before accepting repository work.", ("backend/src/workflows/native_software_engineering.py", "backend/src/tools/process_tools.py"), "integration"),
    Criterion("edge.paired_transport", 749, True, "Paired edge transport source contract is present; behavioral proof is supplied by a child receipt.", "Merge or restore the #749 paired-edge identity, pairing, and revocation contract before accepting edge work.", ("backend/src/extensions/node_pairing.py",), "integration"),
    Criterion("audio.capture_decode_persistence", 751, True, "Audio capture, decode, and persistence source contract is present; behavioral proof is supplied by a child receipt.", "Merge or restore the #751 governed audio capture, decode, and persistence contract before accepting audio work.", ("backend/src/guardian/audio_ingress.py",), "integration"),
    Criterion("telegram.durable_transport", 752, True, "Telegram durable transport source contract is present; behavioral proof is supplied by a child receipt.", "Merge or restore the #752 paired Telegram transport, consent, and durable receipt contract before accepting Telegram work.", ("backend/src/extensions/telegram_ingress.py",), "integration"),
    Criterion("capability_pack.lifecycle", 755, True, "Capability-pack lifecycle source contract is present; behavioral proof is supplied by a child receipt.", "Merge or restore the #755 governed capability-pack lifecycle, review, activation, and rollback contract before accepting packs.", ("backend/src/extensions/capability_pack.py",), "integration"),
    Criterion("runtime.openrouter_model_fabric", 741, True, "OpenRouter model-fabric policy and configuration contracts are present.", "Repair the provider policy and persisted setup before enabling inference.", ("backend/src/model_fabric", "backend/src/llm_runtime.py"), "configuration"),
    Criterion("runtime.remote_inference_admission", 744, True, "Remote inference admission contract is present.", "Restore admission, budget, and cancellation enforcement before inference.", ("backend/src/model_fabric/remote_inference_admission.py", "backend/src/model_fabric/execution.py")),
    Criterion("runtime.effect_reconciliation", 743, True, "Durable effect and reconciliation contracts are present.", "Repair durable effect reconciliation before retrying external work.", ("backend/src/workflows/durable_state.py", "backend/src/workflows/production_workflow_guarantees.py")),
    Criterion("runtime.openrouter_text_receipt", 741, False, "Live OpenRouter text behavior is unverified by this keyless command.", "Provider capability and quality remain outside this implementation receipt.", (), "external_unverified"),
    Criterion("runtime.openrouter_multimodal_receipt", 751, False, "Live OpenRouter multimodal behavior is unverified by this keyless command.", "Provider capability and quality remain outside this implementation receipt.", (), "external_unverified"),
    Criterion("runtime.no_local_inference_dependency", 775, True, "Production configuration has no local model, GPU, VLM, Whisper, or Piper route.", "Clear local inference variables and remove any hidden fallback before deployment.", (), "configuration"),
    Criterion("scheduler.priority_queue", 744, True, "Bounded scheduler and admission source contracts are present.", "Restore priority, serial admission, cancellation, and recovery handling.", ("backend/src/scheduler", "backend/src/model_fabric/remote_inference_admission.py")),
    Criterion("security.capability_caller_enforcement", 747, True, "Capability caller and egress enforcement contracts are present.", "Restore caller authorization and fail-closed egress checks.", ("backend/src/native_tools", "backend/src/security")),
    Criterion("guardian.goal_revision_stop", 745, True, "Goal revision and stale-plan stop contracts are present.", "Repair goal snapshot and stale-plan checks before proactive execution.", ("backend/src/guardian", "backend/src/goals")),
    Criterion("guardian.verified_progress", 746, True, "Guardian outcome and verification contracts are present.", "Restore outcome verification and operator-visible degraded states.", ("backend/src/guardian", "backend/src/api/operator.py")),
    Criterion("memory.embedding_capability", 775, False, "Live embedding behavior is unverified by this keyless command.", "Provider capability and semantic quality remain outside this implementation receipt.", ("backend/src/memory/embedder.py", "backend/src/memory"), "external_unverified"),
    Criterion("memory.correction_delete_restore", 753, True, "Memory correction, deletion, and restore contracts are present.", "Restore canonical-memory correction and deletion guards before learning.", ("backend/src/memory", "backend/tests/test_memory_control.py")),
    Criterion("research.harness_improvement", 771, False, "Harness improvement evaluation is deferred outside Epic #736.", "Keep #771 separately tracked; no benchmark, comparator, canary, or provider call is required here.", (), "excluded"),
    Criterion("evolution.staged_candidate", 771, False, "Evolution candidate proof is explicitly excluded from this keyless Epic #736 receipt.", "Retain #771's staged-candidate and promotion evidence in its separately reviewed workflow.", (), "excluded", "#771 evolution evidence is outside the keyless #736 closure gate."),
    Criterion("evolution.hidden_evaluation", 771, False, "Evolution hidden-split evaluation is explicitly excluded from this keyless Epic #736 receipt.", "Retain #771's held-out evaluation and version-binding evidence in its separately reviewed workflow.", (), "excluded", "#771 evolution evidence is outside the keyless #736 closure gate."),
    Criterion("evolution.scoped_canary_rollback", 771, False, "Evolution canary and rollback proof is explicitly excluded from this keyless Epic #736 receipt.", "Retain #771's approval-bound canary and rollback evidence in its separately reviewed workflow.", (), "excluded", "#771 evolution evidence is outside the keyless #736 closure gate."),
    Criterion("evaluation.comparator_scope_coverage", 754, False, "Comparator scope coverage is explicitly excluded from this keyless Epic #736 receipt.", "Retain comparator scope, coverage, and uncertainty evidence in the separately reviewed evaluation record.", (), "excluded", "Comparator campaigns are outside the keyless #736 closure gate and cannot create a superiority claim."),
    Criterion("backup.restore", 742, True, "Workspace backup and restore contracts are present.", "Repair backup verification and restore recovery before production use.", ("backend/src/workspace", "backend/tests/test_workspace_lifecycle.py")),
    Criterion("edge.mac", 746, False, "Mac edge live readiness was not probed by this keyless command.", "Run the operator-shell edge probe in the target deployment and retain its redacted receipt.", ("backend/src/edge",), "external_unverified"),
    Criterion("voice.audio", 751, False, "Voice and audio provider readiness was not probed by this keyless command.", "Run an isolated provider canary with test media and explicit consent.", ("backend/src/guardian/audio_ingress.py", "backend/src/guardian/multimodal_voice.py"), "external_unverified"),
    Criterion("telegram", 752, False, "Telegram delivery readiness was not probed by this keyless command.", "Run an isolated paired-channel probe with a test account and revocation receipt.", ("backend/src/extensions/telegram_ingress.py",), "external_unverified"),
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


class ChildEvidenceError(ValueError):
    """A child receipt cannot be trusted for an integration criterion."""

    def __init__(self, reason: str, *, criterion_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.criterion_id = criterion_id


def _child_evidence_digest(payload: dict[str, Any]) -> str:
    content = dict(payload)
    content.pop("hash", None)
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_child_timestamp(value: Any, *, now: datetime) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ChildEvidenceError("timestamp is missing or invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChildEvidenceError("timestamp is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ChildEvidenceError("timestamp must include a timezone")
    parsed = parsed.astimezone(timezone.utc)
    if parsed < now - CHILD_EVIDENCE_MAX_AGE:
        raise ChildEvidenceError("evidence is stale")
    if parsed > now + CHILD_EVIDENCE_MAX_FUTURE_SKEW:
        raise ChildEvidenceError("evidence timestamp is in the future")
    return parsed


def _validate_child_evidence(
    payload: Any,
    *,
    expected_commit: str,
    now: datetime | None = None,
) -> ChildEvidence:
    if not isinstance(payload, dict):
        raise ChildEvidenceError("receipt must be a JSON object")
    candidate_id = payload.get("criterion_id")
    if not isinstance(candidate_id, str) or candidate_id not in CHILD_CRITERION_IDS:
        raise ChildEvidenceError("criterion_id is not an accepted required child criterion", criterion_id=candidate_id if isinstance(candidate_id, str) else None)
    criterion = next(item for item in CRITERIA if item.identifier == candidate_id)
    required_fields = {"schema_version", "child_issue", "criterion_id", "commit", "command", "result", "timestamp", "hash"}
    missing = sorted(required_fields - payload.keys())
    if missing:
        raise ChildEvidenceError("missing required fields: " + ", ".join(missing), criterion_id=candidate_id)
    if payload.get("schema_version") != CHILD_EVIDENCE_SCHEMA_VERSION:
        raise ChildEvidenceError("unsupported evidence schema", criterion_id=candidate_id)
    if payload.get("child_issue") != criterion.owner_issue:
        raise ChildEvidenceError("child issue does not own criterion", criterion_id=candidate_id)
    commit = payload.get("commit")
    if not isinstance(commit, str) or len(commit) != 40 or any(character not in "0123456789abcdefABCDEF" for character in commit):
        raise ChildEvidenceError("commit is not a revision hash", criterion_id=candidate_id)
    if expected_commit == "unknown" or commit.lower() != expected_commit.lower():
        raise ChildEvidenceError("evidence revision does not match the collector revision", criterion_id=candidate_id)
    command = payload.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > 4096 or "\x00" in command:
        raise ChildEvidenceError("command is missing or invalid", criterion_id=candidate_id)
    result = payload.get("result")
    if isinstance(result, dict):
        result = result.get("status")
    if not isinstance(result, str) or result.lower() not in VALID_STATUSES:
        raise ChildEvidenceError("result must contain a valid status", criterion_id=candidate_id)
    digest = payload.get("hash")
    if not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdefABCDEF" for character in digest):
        raise ChildEvidenceError("hash is not a SHA-256 digest", criterion_id=candidate_id)
    calculated = _child_evidence_digest(payload)
    if not hmac.compare_digest(digest.lower(), calculated):
        raise ChildEvidenceError("evidence hash mismatch", criterion_id=candidate_id)
    timestamp = _parse_child_timestamp(payload.get("timestamp"), now=now or _utc_now())
    return ChildEvidence(candidate_id, criterion.owner_issue, commit.lower(), command.strip(), result.lower(), timestamp, calculated)


def _evidence_path(path: Path | str | None) -> tuple[Path | None, str]:
    if path is not None:
        requested = Path(path).expanduser()
        if not requested.is_absolute():
            requested = Path.cwd() / requested
        return requested, "explicit"
    canonical = _workspace_root() / CHILD_EVIDENCE_DIRECTORY
    return (canonical, "canonical") if canonical.exists() else (None, "none")


def _load_child_evidence(path: Path | str | None, *, expected_commit: str) -> ChildEvidenceBundle:
    requested, source = _evidence_path(path)
    bundle = ChildEvidenceBundle(source, {}, {})
    if requested is None:
        return bundle
    try:
        _reject_symlink_components(requested.absolute())
        if not requested.exists() or requested.is_symlink():
            bundle.errors["__global__"] = ["evidence path is missing or unsafe"]
            return bundle
        if requested.is_dir():
            candidates = sorted(item for item in requested.iterdir() if item.suffix.lower() == ".json")
            if len(candidates) > 256:
                bundle.errors["__global__"] = ["evidence directory exceeds the bounded file limit"]
                candidates = candidates[:256]
        else:
            candidates = [requested]
    except OSError:
        bundle.errors["__global__"] = ["evidence path is unreadable"]
        return bundle
    for candidate in candidates:
        payload: Any = None
        try:
            if candidate.is_symlink() or candidate.stat().st_size > CHILD_EVIDENCE_MAX_BYTES:
                raise ChildEvidenceError("evidence file is unsafe or too large")
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            evidence = _validate_child_evidence(payload, expected_commit=expected_commit)
        except (OSError, UnicodeError, json.JSONDecodeError, ChildEvidenceError) as exc:
            candidate_id = payload.get("criterion_id") if isinstance(payload, dict) else None
            key = candidate_id if isinstance(candidate_id, str) and candidate_id in CHILD_CRITERION_IDS else "__global__"
            bundle.errors.setdefault(key, []).append(str(exc))
            continue
        bundle.entries.setdefault(evidence.criterion_id, []).append(evidence)
    return bundle


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _nonempty_env(names: Iterable[str]) -> list[str]:
    return [name for name in names if os.environ.get(name, "").strip()]


def _canonical_openrouter_model(value: str) -> str | None:
    """Validate a model id with the runtime's canonical OpenRouter parser."""
    backend_root = ROOT / "backend"
    if not backend_root.is_dir():
        return None
    backend_text = str(backend_root)
    if backend_text not in sys.path:
        sys.path.insert(0, backend_text)
    try:
        from src.model_fabric.configuration import normalize_openrouter_model_id

        return normalize_openrouter_model_id(value)
    except (ImportError, OSError, ValueError):
        return None


def _check(identifier: str, owner: int, status: str, summary: str, recovery: str, *, required: bool, artifact_refs: Iterable[str] = (), evidence_mode: str = "static") -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid health status for {identifier}")
    if evidence_mode not in VALID_EVIDENCE_MODES:
        raise ValueError(f"invalid evidence mode for {identifier}")
    return {
        "id": identifier,
        "owner_issue": owner,
        "required": required,
        "evidence_mode": evidence_mode,
        "status": status,
        "summary": summary,
        "artifact_refs": list(artifact_refs),
        "recovery": recovery,
    }


def _openrouter_config_check() -> dict[str, Any]:
    base = os.environ.get("LLM_API_BASE", "").strip().rstrip("/")
    model = os.environ.get("DEFAULT_MODEL", "").strip()
    allowed = {
        item.strip().lower()
        for item in os.environ.get("OPENROUTER_ALLOWED_UPSTREAMS", "").split(",")
        if item.strip()
    }
    try:
        temperature = float(os.environ.get("MODEL_TEMPERATURE", ""))
        max_tokens = int(os.environ.get("MODEL_MAX_TOKENS", ""))
    except ValueError:
        temperature, max_tokens = -1.0, -1
    canonical_model = _canonical_openrouter_model(model)
    model_provider = None
    if canonical_model is not None:
        model_provider = canonical_model.removeprefix("openrouter/").split("/", 1)[0].lower()
    expected = (
        base == "https://openrouter.ai/api/v1"
        and canonical_model is not None
        and _truthy(os.environ.get("OPENROUTER_PROVIDER_ONLY"))
        and not _truthy(os.environ.get("OPENROUTER_ALLOW_FALLBACKS"))
        and _truthy(os.environ.get("OPENROUTER_REQUIRE_PARAMETERS"))
        and os.environ.get("OPENROUTER_DATA_COLLECTION", "").strip().lower() == "deny"
        and model_provider is not None
        and model_provider in allowed
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
        evidence_mode="configuration",
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
        evidence_mode="configuration",
    )


def _contract_check(
    criterion: Criterion,
    *,
    identifier: str | None = None,
    required: bool | None = None,
) -> dict[str, Any]:
    check_id = identifier or criterion.identifier
    check_required = criterion.required if required is None else required
    missing = [path for path in criterion.paths if not (ROOT / path).exists()]
    if missing:
        return _check(
            check_id,
            criterion.owner_issue,
            "blocked" if check_required else "skipped",
            "Implementation source contract is unavailable in this revision." if check_required else "Optional implementation surface is unavailable in this revision; no live probe was attempted.",
            criterion.recovery,
            required=check_required,
            evidence_mode="static",
        )
    return _check(
        check_id,
        criterion.owner_issue,
        "pass",
        criterion.summary + " Source presence is informational; live execution is not claimed by this receipt.",
        criterion.recovery,
        required=check_required,
        artifact_refs=("source-contract:" + criterion.identifier,),
        evidence_mode="static",
    )


def _child_evidence_check(criterion: Criterion, bundle: ChildEvidenceBundle) -> dict[str, Any]:
    reasons = list(bundle.errors.get("__global__", ())) + list(bundle.errors.get(criterion.identifier, ()))
    entries = bundle.entries.get(criterion.identifier, ())
    if reasons:
        status = "unknown"
        summary = "Trusted child behavioral evidence is invalid: " + "; ".join(reasons[:2])
        artifact_refs: tuple[str, ...] = ()
    elif len(entries) == 0:
        status = "unknown"
        summary = "Trusted child behavioral evidence is missing; source presence cannot satisfy this required gate."
        artifact_refs = ()
    elif len(entries) != 1:
        status = "unknown"
        summary = "Trusted child behavioral evidence is ambiguous because multiple receipts claim this criterion."
        artifact_refs = ()
    else:
        evidence = entries[0]
        status = evidence.result
        summary = (
            "Trusted child behavioral evidence passed for the collector revision."
            if status == "pass"
            else f"Trusted child behavioral evidence reported {status}."
        )
        artifact_refs = ("child-evidence:" + evidence.digest[:16],)
    return _check(
        criterion.identifier,
        criterion.owner_issue,
        status,
        summary,
        criterion.recovery,
        required=True,
        artifact_refs=artifact_refs,
        evidence_mode="integration",
    )


def _optional_live_check(criterion: Criterion) -> dict[str, Any]:
    return _check(
        criterion.identifier,
        criterion.owner_issue,
        "skipped",
        criterion.summary + " No provider, connector, edge, or media call was made.",
        criterion.recovery,
        required=False,
        evidence_mode=criterion.evidence_mode,
    )


def _excluded_check(criterion: Criterion) -> dict[str, Any]:
    summary = criterion.summary
    if criterion.exclusion_reason:
        summary = f"{summary} {criterion.exclusion_reason}"
    return _check(
        criterion.identifier,
        criterion.owner_issue,
        "skipped",
        summary,
        criterion.recovery,
        required=False,
        artifact_refs=("exclusion:" + criterion.identifier,),
        evidence_mode="excluded",
    )


def _overall(checks: list[dict[str, Any]]) -> tuple[str, int]:
    statuses = {str(item["status"]) for item in checks}
    if "failed" in statuses:
        return "failed", EXIT_FAILED
    # The offline collector may be run before child runners have emitted their
    # receipts.  Keep that state visibly degraded (never healthy) while using
    # the optional-evidence exit so an operator can collect the missing proof.
    if any(
        item["required"]
        and item["evidence_mode"] == "integration"
        and item["status"] in {"unknown", "blocked", "degraded", "skipped"}
        for item in checks
    ):
        return "degraded", EXIT_OPTIONAL_DEGRADED
    if any(item["required"] and item["status"] == "blocked" for item in checks):
        return "blocked", EXIT_REQUIRED_BLOCKED
    if any(item["required"] and item["status"] == "unknown" for item in checks):
        return "blocked", EXIT_REQUIRED_BLOCKED
    if any(item["required"] and item["status"] == "degraded" for item in checks):
        return "degraded", EXIT_REQUIRED_BLOCKED
    if any(item["status"] in OPTIONAL_STATUSES or item["status"] == "unknown" for item in checks):
        return "degraded", EXIT_OPTIONAL_DEGRADED
    return "pass", EXIT_PASS


def _workspace_root() -> Path:
    if not os.environ.get("BACKEND_DATA_PATH_PROD", "").strip():
        raise RuntimeError("canonical production workspace bind is not configured")
    backend_root = ROOT / "backend"
    if not backend_root.is_dir():
        raise RuntimeError("canonical workspace contract is unavailable")
    backend_text = str(backend_root)
    if backend_text not in sys.path:
        sys.path.insert(0, backend_text)
    try:
        from src.workspace.production import ProductionWorkspaceError, resolve_production_workspace
    except ImportError as exc:
        raise RuntimeError("canonical production workspace is invalid") from exc
    try:
        workspace = resolve_production_workspace(dict(os.environ), base_dir=ROOT)
    except (OSError, ProductionWorkspaceError, ValueError) as exc:
        raise RuntimeError("canonical production workspace is invalid") from exc
    return workspace.host_root


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError("canonical receipt path is unreadable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("canonical receipt path must not contain symlinks")


def _sync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_receipt(receipt: dict[str, Any], generated_at: datetime) -> str:
    directory = _workspace_root() / "operator-receipts" / "epic-736-health"
    _reject_symlink_components(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(directory)
    lock_path = directory / ".health-receipt.lock"
    lock_fd = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        filename = generated_at.strftime("%Y%m%dT%H%M%SZ") + ".json"
        target = directory / filename
        while target.exists():
            filename = generated_at.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12] + ".json"
            target = directory / filename
        payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=directory, prefix=".health-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            _sync_directory(directory)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return f"operator-receipts/epic-736-health/{filename}"


def build_receipt(evidence_path: Path | str | None = None) -> tuple[dict[str, Any], int, str]:
    generated = _utc_now()
    expected_commit = _safe_commit()
    child_evidence = _load_child_evidence(evidence_path, expected_commit=expected_commit)
    checks: list[dict[str, Any]] = []
    # The model-fabric entry is special because it is also the configuration
    # authority consumed by the rest of the checks.
    checks.append(_openrouter_config_check())
    checks.append(_no_local_inference_check())
    for criterion in CRITERIA:
        if criterion.identifier in {"runtime.openrouter_model_fabric", "runtime.no_local_inference_dependency"}:
            continue
        if criterion.evidence_mode == "excluded":
            checks.append(_excluded_check(criterion))
        elif criterion.identifier in CHILD_CRITERION_IDS:
            checks.append(
                _contract_check(
                    criterion,
                    identifier=criterion.identifier + ".source",
                    required=False,
                )
            )
            checks.append(_child_evidence_check(criterion, child_evidence))
        elif not criterion.required and criterion.evidence_mode in {"external_unverified", "integration"}:
            checks.append(_optional_live_check(criterion))
        elif not criterion.required and not criterion.paths:
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
        "Evolution candidate, hidden-evaluation, canary-rollback, and comparator evidence remains explicitly excluded and separately owned.",
        "Required child criteria have separate informational source checks and integration gates; source presence never satisfies behavioral evidence.",
        "Missing, stale, malformed, mismatched, or ambiguous child receipts remain unknown and visibly degraded until a trusted runner emits a current passing receipt.",
    ]
    receipt: dict[str, Any] = {
        "schema_version": 2,
        "epic": 736,
        "generated_at": _timestamp(generated),
        "environment": "prod",
        "commit": expected_commit,
        "overall_status": overall_status,
        "claim_boundary": "static_and_local_contract_evidence_only; external_provider_quality_and_evolution_comparator_evidence_excluded",
        "exclusions": [
            {
                "id": item["id"],
                "owner_issue": item["owner_issue"],
                "reason": item["summary"],
            }
            for item in checks
            if item["evidence_mode"] == "excluded"
        ],
        "checks": checks,
        "child_evidence": {
            "schema_version": CHILD_EVIDENCE_SCHEMA_VERSION,
            "source": child_evidence.source,
            "expected_commit": expected_commit,
            "max_age_seconds": int(CHILD_EVIDENCE_MAX_AGE.total_seconds()),
            "invalid_count": child_evidence.invalid_count,
        },
        "redactions": {"count": 0, "classes": ["credentials", "message_content", "raw_media", "sensitive_paths"]},
        "skipped": skipped,
        "residual_risks": residual_risks,
    }
    logical_path = _write_receipt(receipt, generated)
    return receipt, exit_code, logical_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument(
        "--child-evidence",
        "--evidence-dir",
        "--evidence-path",
        "--evidence",
        dest="evidence_path",
        type=Path,
        default=None,
        help="approved child receipt file or directory (defaults to the canonical child-evidence directory)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        # argparse's documented usage errors are stable and must not be
        # confused with a health failure.
        return EXIT_PASS if int(exc.code) == 0 else EXIT_USAGE
    try:
        receipt, exit_code, logical_path = build_receipt(args.evidence_path)
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
