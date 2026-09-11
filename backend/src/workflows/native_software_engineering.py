"""Deterministic, offline software-engineering workflow fixture.

This module composes the existing durable job record, native process runtime,
bounded filesystem patch tool, and artifact registry into one small vertical
slice.  It intentionally has no model/provider dependency and does not expose
an arbitrary shell or repository mutation surface.

The fixture is copied into a job-owned workspace before any patch is applied.
Caller-supplied trees may be used when they are already inside the configured
workspace, pass the untrusted-input scan, and match the documented fixture
shape; sources outside that boundary remain inspection-only. This slice does
not claim a general sandbox or network isolation boundary. Planner, worker,
and critic labels are metadata only; the durable job owner remains the sole
authority identity.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from config.settings import settings
from src.approval.identity import approval_owner_operator_session_id
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.approval.runtime import (
    get_current_session_id,
    get_current_trust_principal,
    reset_runtime_fencing_token,
    reset_runtime_trust_principal,
    set_runtime_fencing_token,
    set_runtime_trust_principal,
    verify_capability_approval,
)
from src.artifacts.registry import build_artifact_record
from src.extensions.capability_execution import (
    CapabilityExecutionError,
    _RAW_RESULT_TOKEN,
    build_capability_request,
    current_capability_execution_host,
)
from src.security.trust_contract import AuthorityGrant, PrincipalType
from src.tools.filesystem_tool import _safe_resolve, _write_workspace_text_bounded
from src.workflows.job_runtime import (
    DurableJobIdentity,
    DurableJobLeaseError,
    DurableJobSpec,
    durable_job_repository,
)


NATIVE_SOFTWARE_ENGINEERING_CAPABILITY_ID = "native_software_engineering"
NATIVE_SOFTWARE_ENGINEERING_CAPABILITY_VERSION = "1"
NATIVE_SOFTWARE_ENGINEERING_JOB_KIND = "native_software_engineering_fixture"
NATIVE_SOFTWARE_ENGINEERING_IDEMPOTENCY_SCOPE = "native_software_engineering_fixture"
NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE = "deterministic_offline_fixture"

FIXTURE_BUG_FILE = "calculator.py"
FIXTURE_TEST_FILE = "tests/test_calculator.py"
FIXTURE_BEFORE_TEXT = "return left - right"
FIXTURE_AFTER_TEXT = "return left + right"
FIXTURE_TEST_COMMAND = "pytest"
FIXTURE_TEST_ARGS = ("-q", FIXTURE_TEST_FILE)
_PATCH_APPROVAL_STATES = frozenset({"approved", "required", "denied"})
_NATIVE_PATCH_CAPABILITY_ID = "apply_workspace_patch"
_NATIVE_APPROVAL_RISK = "high"
_NATIVE_APPROVAL_TTL_SECONDS = 5 * 60.0

_MAX_FIXTURE_FILES = 200
_MAX_FIXTURE_FILE_BYTES = 1_000_000
_MAX_FIXTURE_TOTAL_BYTES = 8_000_000
_MAX_FIXTURE_DEPTH = 16
_MAX_FIXTURE_DIRECTORIES = 200
_MAX_TEST_TIMEOUT_SECONDS = 300
_MAX_NATIVE_ATTEMPTS = 2
_MAX_ARTIFACT_BYTES = 1 * 1024 * 1024
_GENERATED_FIXTURE_DIRS = frozenset({".git", "__pycache__", ".pytest_cache"})
_NATIVE_CANCEL_SETTLE_SECONDS = 1.0
_NATIVE_CANCEL_POLL_SECONDS = 0.02
_NATIVE_CANCEL_CAS_ATTEMPTS = 6
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SHELL_META_CHARS = set("|&;<>()`$\\\n\r\t")
_ROLE_IDENTITIES = frozenset(
    {
        "planner",
        "worker",
        "critic",
        "role:planner",
        "role:worker",
        "role:critic",
    }
)
_SECRET_NAMES = frozenset(
    {
        ".env",
        ".envrc",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "private_key",
        "secrets.json",
    }
)
_SECRET_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
_SECRET_PATH_PARTS = frozenset({".aws", ".azure", ".docker", ".gnupg", ".ssh"})
_UNTRUSTED_INSTRUCTION_MARKERS = (
    "ignore previous",
    "ignore all instructions",
    "ignore policy",
    "exfiltrate",
    "read .env",
    "cat .env",
    "print the secret",
    "print secret",
    "api key",
    "private key",
    "rm -rf",
    "sudo ",
    "curl ",
    "wget ",
    "ssh ",
    "git push",
    "deploy",
    "import socket",
    "import requests",
    "import httpx",
    "urllib.request",
)

_NATIVE_CANCEL_EVENT: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "native_software_engineering_cancel_event",
    default=None,
)
_NATIVE_JOB_ID: contextvars.ContextVar[str] = contextvars.ContextVar("native_software_engineering_job_id", default="")
_NATIVE_FENCING_TOKEN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "native_software_engineering_fencing_token", default=""
)
_NATIVE_EXECUTION_LOCK = threading.Lock()
_NATIVE_EXECUTIONS: dict[str, "_NativeExecutionControl"] = {}


class NativeSoftwareEngineeringError(ValueError):
    """A deterministic fixture request was rejected before or during execution."""

    def __init__(self, reason_code: str, message: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


@dataclass(frozen=True, slots=True)
class NativeSoftwareEngineeringApprovalReceipt:
    """Legacy caller-shaped evidence retained only for read compatibility.

    This value is never an authority source. Apply authority comes from a
    consumed row in :mod:`src.approval.repository` and its runtime binding.
    """

    receipt_id: str
    owner_principal_id: str
    session_id: str
    job_id: str
    preview_digest: str
    expires_at: float
    action: str = "apply"
    decision: str = "approved"
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class NativeSoftwareEngineeringRequest:
    """Inputs for one bounded fixture invocation.

    ``planner_id``, ``worker_id``, and ``critic_id`` are retained as lineage
    labels.  They never become the durable owner or grant execution authority.
    """

    fixture_root: str | Path | None = None
    job_id: str = "native-swe-fixture-1"
    session_id: str = "native-swe-fixture-session"
    owner_kind: str = "service"
    owner_principal_id: str = "service:native-software-engineering"
    service_id: str | None = "service:native-software-engineering"
    idempotency_key: str | None = None
    planner_id: str = "planner:deterministic"
    worker_id: str = "worker:deterministic"
    critic_id: str = "critic:deterministic"
    priority: int = 50
    deadline_at: datetime | str | None = None
    test_command: str = FIXTURE_TEST_COMMAND
    test_args: tuple[str, ...] = field(default_factory=lambda: FIXTURE_TEST_ARGS)
    test_timeout_seconds: int = 30
    max_attempts: int = 2
    # A caller that separates inspect/approval/apply can bind the later run to
    # the exact source tree it inspected.  Omitting this remains valid for the
    # one-shot local fixture path, which computes and checks its own digest.
    expected_source_digest: str | None = None
    # Approval is required by default. A caller must provide an explicit
    # bounded approval decision before the patch can be applied.
    patch_approval: str = "required"
    approval_receipt: NativeSoftwareEngineeringApprovalReceipt | None = None
    # ``approval_id`` identifies the durable ApprovalRequest selected by an
    # authenticated operator. The row and its signed runtime binding are
    # checked again immediately before the patch effect.
    approval_id: str | None = None
    # A stable attempt label is included in the approval context. It is
    # derived when omitted and therefore cannot grant authority by itself.
    attempt_id: str | None = None
    instruction_text: str = ""
    cancel_before_test: bool = False


@dataclass(frozen=True, slots=True)
class _PreparedFixture:
    source: Path
    source_digest: str
    bug_path: Path
    test_path: Path


@dataclass(frozen=True, slots=True)
class _JobWorkspace:
    root: Path
    relative_root: str
    branch: str
    artifact_dir: Path
    relative_artifact_dir: str


@dataclass(slots=True)
class _NativeExecutionControl:
    owner: str
    fencing_token: int
    cancel_event: threading.Event
    cleanup_requested: bool = False
    cleanup_status: str = "not_requested"
    remaining_descendants: int | None = None
    worker_root: str | None = None


def native_software_engineering_fixture_root() -> Path:
    """Return the bundled source tree used by deterministic proof tests."""
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "native_swe_repo"


def _workspace_root() -> Path:
    return Path(settings.workspace_dir).resolve()


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_text(value: str) -> str:
    return _digest_bytes(value.encode("utf-8"))


def _preview_scope_digest(request: NativeSoftwareEngineeringRequest) -> str:
    return _digest(
        {
            "action": "apply",
            "job_id": request.job_id,
            "session_id": request.session_id,
            "file_path": FIXTURE_BUG_FILE,
            "before_sha256": _digest_text(FIXTURE_BEFORE_TEXT),
            "after_sha256": _digest_text(FIXTURE_AFTER_TEXT),
            "expected_source_digest": request.expected_source_digest or "",
        }
    )


def _native_attempt_id(request: NativeSoftwareEngineeringRequest) -> str:
    """Return the stable attempt binding used by approval and effect records."""
    candidate = str(request.attempt_id or f"native-swe-attempt:{_job_token(request.job_id)}").strip()
    if not _SAFE_JOB_ID.fullmatch(candidate):
        raise NativeSoftwareEngineeringError("attempt_identity_invalid")
    return candidate


def _native_patch_arguments(
    relative_bug_path: str,
    *,
    preview_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the exact immutable arguments sent to the patch capability."""
    before_sha256 = str(preview_payload.get("before_sha256") or "")
    if not before_sha256:
        raise NativeSoftwareEngineeringError("patch_preview_digest_missing")
    return {
        "file_path": relative_bug_path,
        "old_text": FIXTURE_BEFORE_TEXT,
        "new_text": FIXTURE_AFTER_TEXT,
        "expected_occurrences": 1,
        "expected_before_sha256": before_sha256,
    }


def _native_approval_context(
    request: NativeSoftwareEngineeringRequest,
    prepared: _PreparedFixture,
    *,
    relative_bug_path: str,
    preview_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Describe every mutable input that an operator is approving.

    The context is persisted inside the ApprovalRequest and included in its
    fingerprint. It is deliberately structural and provider-free so the same
    row can be checked by the approval repository and the capability host.
    """
    patch_arguments = _native_patch_arguments(relative_bug_path, preview_payload=preview_payload)
    return {
        "schema_version": "seraph.native-software-engineering.approval-context.v2",
        "owner": {
            "kind": request.owner_kind,
            "principal_id": request.owner_principal_id,
            "service_id": request.service_id or "",
        },
        "session_id": request.session_id,
        "job_id": request.job_id,
        "attempt_id": _native_attempt_id(request),
        "source_digest": prepared.source_digest,
        "patch": {
            "capability_id": _NATIVE_PATCH_CAPABILITY_ID,
            "destination": f"workspace:{relative_bug_path}",
            "arguments": patch_arguments,
            "file_path": relative_bug_path,
            "before_sha256": str(preview_payload.get("before_sha256") or ""),
            "after_sha256": str(preview_payload.get("after_sha256") or ""),
        },
        "verification": {
            "command": request.test_command,
            "args": list(request.test_args),
            "timeout_seconds": int(request.test_timeout_seconds),
            "max_attempts": int(request.max_attempts),
        },
        "resources": {
            "priority": int(request.priority),
            "claims": ["cpu", "workspace"],
        },
    }


def _native_approval_fingerprint(
    approval_context: Mapping[str, Any],
) -> str:
    patch = approval_context.get("patch")
    if not isinstance(patch, Mapping):
        raise NativeSoftwareEngineeringError("approval_context_invalid")
    arguments = patch.get("arguments")
    if not isinstance(arguments, Mapping):
        raise NativeSoftwareEngineeringError("approval_context_invalid")
    return fingerprint_tool_call(
        _NATIVE_PATCH_CAPABILITY_ID,
        dict(arguments),
        approval_context=dict(approval_context),
    )


def _native_approval_owner_session(runtime_principal: Any, *, session_id: str) -> str:
    owner_session = approval_owner_operator_session_id(
        session_id=session_id,
        principal=runtime_principal,
    )
    if not owner_session:
        raise NativeSoftwareEngineeringError("approval_operator_session_missing")
    return owner_session


def build_native_software_engineering_approval_receipt(
    request: NativeSoftwareEngineeringRequest,
    *,
    receipt_id: str | None = None,
    expires_at: float | None = None,
) -> NativeSoftwareEngineeringApprovalReceipt:
    """Reject the historical caller-fabricated approval helper.

    Kept as a compatibility symbol so old integrations fail explicitly at the
    boundary instead of silently manufacturing authority. Use the approval
    API/repository to create and resolve an ``ApprovalRequest`` row, then pass
    its ``approval_id`` to the native runner.
    """
    del request, receipt_id, expires_at
    raise NativeSoftwareEngineeringError("approval_repository_required")


def _safe_relative(path: Path, root: Path, *, reason_code: str) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise NativeSoftwareEngineeringError(reason_code) from exc


def _is_secret_like(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    parts = {part.lower() for part in relative.parts}
    if parts & _SECRET_PATH_PARTS:
        return True
    name = path.name.lower()
    return (
        name in _SECRET_NAMES
        or name.startswith(".env.")
        or name.endswith(_SECRET_SUFFIXES)
        or any(token in name for token in ("credential", "secret", "token"))
    )


def _fixture_files(root: Path) -> list[Path]:
    files: list[Path] = []
    total_bytes = 0
    directory_count = 0
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        try:
            current_depth = len(current_path.relative_to(root).parts)
        except ValueError as exc:
            raise NativeSoftwareEngineeringError("fixture_scope_invalid") from exc
        if current_depth > _MAX_FIXTURE_DEPTH:
            raise NativeSoftwareEngineeringError("fixture_depth_exceeded")
        dirnames[:] = sorted(dirnames)
        filenames = sorted(filenames)
        dirnames[:] = [name for name in dirnames if name not in _GENERATED_FIXTURE_DIRS]
        directory_count += len(dirnames)
        if directory_count > _MAX_FIXTURE_DIRECTORIES:
            raise NativeSoftwareEngineeringError("fixture_directory_count_exceeded")
        for name in (*dirnames, *filenames):
            candidate = current_path / name
            if candidate.is_symlink():
                raise NativeSoftwareEngineeringError("fixture_symlink_blocked")
            if name in dirnames and not candidate.is_dir():
                raise NativeSoftwareEngineeringError("fixture_directory_invalid")
        for name in filenames:
            candidate = current_path / name
            if candidate.is_symlink():
                raise NativeSoftwareEngineeringError("fixture_symlink_blocked")
            try:
                candidate_stat = candidate.stat()
            except OSError as exc:
                raise NativeSoftwareEngineeringError("fixture_read_blocked") from exc
            if not stat.S_ISREG(candidate_stat.st_mode):
                raise NativeSoftwareEngineeringError("fixture_non_regular_file_blocked")
            if candidate_stat.st_size > _MAX_FIXTURE_FILE_BYTES:
                raise NativeSoftwareEngineeringError("fixture_file_size_exceeded")
            total_bytes += candidate_stat.st_size
            if total_bytes > _MAX_FIXTURE_TOTAL_BYTES:
                raise NativeSoftwareEngineeringError("fixture_total_size_exceeded")
            files.append(candidate)
    if len(files) > _MAX_FIXTURE_FILES:
        raise NativeSoftwareEngineeringError("fixture_file_count_exceeded")
    return files


def _fixture_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _fixture_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            try:
                opened_stat = os.fstat(fd)
                if not stat.S_ISREG(opened_stat.st_mode):
                    raise NativeSoftwareEngineeringError("fixture_non_regular_file_blocked")
                content = os.read(fd, _MAX_FIXTURE_FILE_BYTES + 1)
            finally:
                os.close(fd)
        except NativeSoftwareEngineeringError:
            raise
        except OSError as exc:
            raise NativeSoftwareEngineeringError("fixture_read_blocked") from exc
        if len(content) > _MAX_FIXTURE_FILE_BYTES:
            raise NativeSoftwareEngineeringError("fixture_file_size_exceeded")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _scan_untrusted_fixture(root: Path, *, instruction_text: str = "") -> None:
    for candidate in _fixture_files(root):
        if _is_secret_like(candidate, root):
            raise NativeSoftwareEngineeringError("fixture_secret_path_blocked")
        try:
            body = candidate.read_bytes()[: _MAX_FIXTURE_FILE_BYTES + 1].decode("utf-8", errors="ignore").lower()
        except OSError as exc:
            raise NativeSoftwareEngineeringError("fixture_read_blocked") from exc
        for marker in _UNTRUSTED_INSTRUCTION_MARKERS:
            if marker in body:
                raise NativeSoftwareEngineeringError("untrusted_instruction_blocked")
    lowered_instruction = str(instruction_text or "").lower()
    if any(marker in lowered_instruction for marker in _UNTRUSTED_INSTRUCTION_MARKERS):
        raise NativeSoftwareEngineeringError("untrusted_instruction_blocked")


def _validate_relative_fixture_file(root: Path, relative: str, *, reason_code: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not relative.strip():
        raise NativeSoftwareEngineeringError(reason_code)
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise NativeSoftwareEngineeringError(reason_code) from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise NativeSoftwareEngineeringError(reason_code)
    if _is_secret_like(resolved, root):
        raise NativeSoftwareEngineeringError("fixture_secret_path_blocked")
    return resolved


def _validate_request_identity(request: NativeSoftwareEngineeringRequest) -> None:
    if not _SAFE_JOB_ID.fullmatch(str(request.job_id or "")):
        raise NativeSoftwareEngineeringError("job_identity_invalid")
    if not str(request.session_id or "").strip():
        raise NativeSoftwareEngineeringError("session_identity_missing")
    if request.owner_kind not in {"user", "service"} or not str(request.owner_principal_id or "").strip():
        raise NativeSoftwareEngineeringError("owner_identity_invalid")
    if str(request.owner_principal_id).strip().lower() in _ROLE_IDENTITIES:
        raise NativeSoftwareEngineeringError("role_identity_cannot_authorize")
    if request.owner_kind == "service" and not str(request.service_id or "").strip():
        raise NativeSoftwareEngineeringError("service_identity_missing")
    if request.owner_kind == "user" and str(request.service_id or "").strip():
        raise NativeSoftwareEngineeringError("user_service_identity_mismatch")
    for label in (request.planner_id, request.worker_id, request.critic_id):
        if not str(label or "").strip() or any(char in str(label) for char in _SHELL_META_CHARS):
            raise NativeSoftwareEngineeringError("actor_identity_invalid")
    _native_attempt_id(request)

    current_principal = get_current_trust_principal()
    if current_principal is None:
        raise NativeSoftwareEngineeringError("runtime_principal_missing")
    if request.owner_kind != "service" or request.owner_principal_id != "service:native-software-engineering":
        raise NativeSoftwareEngineeringError("native_service_owner_required")
    if current_principal.principal_type != PrincipalType.SERVICE:
        raise NativeSoftwareEngineeringError("runtime_principal_type_invalid")
    if current_principal.principal_id != request.owner_principal_id:
        raise NativeSoftwareEngineeringError("runtime_principal_owner_mismatch")
    if current_principal.session_id != request.session_id or get_current_session_id() != request.session_id:
        raise NativeSoftwareEngineeringError("runtime_session_owner_mismatch")
    if not current_principal.authenticated or current_principal.revoked:
        raise NativeSoftwareEngineeringError("runtime_principal_not_authorized")
    grants = {str(getattr(grant, "value", grant)) for grant in current_principal.grants}
    if AuthorityGrant.CAPABILITY_EXECUTE.value not in grants:
        raise NativeSoftwareEngineeringError("capability_execution_grant_missing")


def _validate_test_command(request: NativeSoftwareEngineeringRequest) -> None:
    if request.test_command != FIXTURE_TEST_COMMAND:
        raise NativeSoftwareEngineeringError("test_command_not_allowlisted")
    if not 1 <= int(request.test_timeout_seconds) <= _MAX_TEST_TIMEOUT_SECONDS:
        raise NativeSoftwareEngineeringError("test_timeout_out_of_bounds")
    if not 1 <= int(request.max_attempts) <= _MAX_NATIVE_ATTEMPTS:
        raise NativeSoftwareEngineeringError("attempt_limit_out_of_bounds")
    if not request.test_args:
        raise NativeSoftwareEngineeringError("test_arguments_missing")
    if tuple(request.test_args) != FIXTURE_TEST_ARGS:
        raise NativeSoftwareEngineeringError("test_arguments_not_allowlisted")
    for arg in request.test_args:
        if not isinstance(arg, str) or not arg.strip() or any(char in arg for char in _SHELL_META_CHARS):
            raise NativeSoftwareEngineeringError("test_argument_blocked")
        path_like = arg.split("=", 1)[-1] if arg.startswith("-") and "=" in arg else arg
        parsed = Path(path_like)
        if parsed.is_absolute() or ".." in parsed.parts:
            raise NativeSoftwareEngineeringError("test_path_outside_workspace")


def _validate_patch_approval(request: NativeSoftwareEngineeringRequest) -> None:
    if request.patch_approval not in _PATCH_APPROVAL_STATES:
        raise NativeSoftwareEngineeringError("patch_approval_state_invalid")
    receipt = request.approval_receipt
    if request.patch_approval != "approved":
        if receipt is not None or request.approval_id is not None:
            raise NativeSoftwareEngineeringError("approval_binding_state_mismatch")
        return
    if receipt is not None:
        # The old dataclass is intentionally not accepted as authority. It is
        # caller-controlled data and has no repository CAS or runtime MAC.
        raise NativeSoftwareEngineeringError("approval_receipt_unsupported")
    approval_id = str(request.approval_id or "").strip()
    if not approval_id:
        raise NativeSoftwareEngineeringError("approval_id_required")
    if not _SAFE_JOB_ID.fullmatch(approval_id):
        raise NativeSoftwareEngineeringError("approval_id_invalid")


def _prepare_fixture(request: NativeSoftwareEngineeringRequest, *, executable: bool = False) -> _PreparedFixture:
    _validate_request_identity(request)
    _validate_test_command(request)
    _validate_patch_approval(request)
    source = Path(request.fixture_root).expanduser() if request.fixture_root is not None else native_software_engineering_fixture_root()
    source = source.resolve()
    workspace = _workspace_root()
    bundled = native_software_engineering_fixture_root().resolve()
    try:
        source.relative_to(workspace)
    except ValueError:
        if source != bundled:
            raise NativeSoftwareEngineeringError("fixture_outside_workspace")
    if not source.is_dir() or source.is_symlink():
        raise NativeSoftwareEngineeringError("fixture_root_invalid")
    _scan_untrusted_fixture(source, instruction_text=request.instruction_text)
    source_digest = _fixture_tree_digest(source)
    if request.expected_source_digest and source_digest != request.expected_source_digest:
        raise NativeSoftwareEngineeringError("fixture_source_digest_mismatch")
    bug_path = _validate_relative_fixture_file(source, FIXTURE_BUG_FILE, reason_code="fixture_bug_file_invalid")
    test_path = _validate_relative_fixture_file(source, FIXTURE_TEST_FILE, reason_code="fixture_test_file_invalid")
    body = bug_path.read_text(encoding="utf-8")
    if body.count(FIXTURE_BEFORE_TEXT) != 1:
        raise NativeSoftwareEngineeringError("fixture_bug_not_reproducible")
    readme = source / "README.md"
    if not readme.is_file():
        raise NativeSoftwareEngineeringError("fixture_bug_documentation_missing")
    readme_body = readme.read_text(encoding="utf-8").lower()
    if "bug" not in readme_body or "subtract" not in readme_body:
        raise NativeSoftwareEngineeringError("fixture_bug_documentation_missing")
    return _PreparedFixture(source=source, source_digest=source_digest, bug_path=bug_path, test_path=test_path)


def preflight_native_software_engineering_fixture(
    request: NativeSoftwareEngineeringRequest | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Inspect the fixture and return a redacted, deterministic preflight receipt."""
    request = _request_from_inputs(request, overrides)
    try:
        prepared = _prepare_fixture(request)
    except NativeSoftwareEngineeringError as exc:
        return {
            "status": "blocked",
            "reason_code": exc.reason_code,
            "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
            "provider": None,
            "operator_visible": True,
        }
    return {
        "status": "ready",
        "reason_code": "fixture_inspected",
        "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
        "provider": None,
        "fixture_name": NATIVE_SOFTWARE_ENGINEERING_CAPABILITY_ID,
        "source_digest": prepared.source_digest,
        "bug": {
            "file_path": FIXTURE_BUG_FILE,
            "test_path": FIXTURE_TEST_FILE,
            "before_sha256": _digest_text(FIXTURE_BEFORE_TEXT),
            "after_sha256": _digest_text(FIXTURE_AFTER_TEXT),
        },
        "operator_visible": True,
    }


def build_native_software_engineering_plan(
    request: NativeSoftwareEngineeringRequest,
    inspection: dict[str, Any],
) -> dict[str, Any]:
    """Build the fixed plan artifact; no model output is consulted."""
    return {
        "schema_version": "seraph.native-software-engineering.plan.v1",
        "capability_id": NATIVE_SOFTWARE_ENGINEERING_CAPABILITY_ID,
        "capability_version": NATIVE_SOFTWARE_ENGINEERING_CAPABILITY_VERSION,
        "job_id": request.job_id,
        "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
        "provider": None,
        "steps": ["inspect", "plan", "preview", "approval", "apply", "test", "diagnose", "readback"],
        "fixture": {
            "file_path": FIXTURE_BUG_FILE,
            "test_path": FIXTURE_TEST_FILE,
            "before_sha256": inspection["bug"]["before_sha256"],
            "after_sha256": inspection["bug"]["after_sha256"],
            "source_digest": inspection["source_digest"],
        },
        "test": {
            "command": request.test_command,
            "argument_count": len(request.test_args),
            "timeout_seconds": request.test_timeout_seconds,
            "max_attempts": request.max_attempts,
        },
        "approval": {
            "required": True,
            "state": request.patch_approval,
            "boundary": "preview_then_approval_then_apply",
            "source": (
                "repository_approval_binding"
                if request.patch_approval == "approved"
                else "approval_request_repository"
            ),
        },
        "authority": {
            "owner_principal_id": request.owner_principal_id,
            "session_id": request.session_id,
            "job_id": request.job_id,
            "attempt_id": _native_attempt_id(request),
            "approval_id": request.approval_id,
            "planner_id": request.planner_id,
            "worker_id": request.worker_id,
            "critic_id": request.critic_id,
            "actor_roles_are_metadata_only": True,
            "authority_source": "durable_job_owner",
        },
        "policy": {
            "workspace_scoped": True,
            "original_fixture_mutation": "blocked_and_digest_checked",
            "network": "not_claimed; execution is restricted to the bundled fixture",
            "secrets": "blocked",
            "external_mutation": "excluded",
        },
    }


def _request_from_inputs(
    request: NativeSoftwareEngineeringRequest | None,
    overrides: dict[str, Any],
) -> NativeSoftwareEngineeringRequest:
    if request is not None:
        if overrides:
            raise ValueError("request and keyword overrides cannot be combined")
        return request
    return NativeSoftwareEngineeringRequest(**overrides)


def _job_token(job_id: str) -> str:
    return hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:24]


def _register_native_execution(job_id: str, control: _NativeExecutionControl) -> None:
    with _NATIVE_EXECUTION_LOCK:
        _NATIVE_EXECUTIONS[job_id] = control


def _unregister_native_execution(job_id: str, control: _NativeExecutionControl | None) -> None:
    if control is None:
        return
    with _NATIVE_EXECUTION_LOCK:
        if _NATIVE_EXECUTIONS.get(job_id) is control:
            _NATIVE_EXECUTIONS.pop(job_id, None)


def _native_execution_for_job(job_id: str) -> _NativeExecutionControl | None:
    with _NATIVE_EXECUTION_LOCK:
        return _NATIVE_EXECUTIONS.get(job_id)


def _mark_native_cleanup_requested(
    job_id: str,
    control: _NativeExecutionControl | None = None,
) -> None:
    """Record cancellation intent before the process result is available."""
    with _NATIVE_EXECUTION_LOCK:
        current = control or _NATIVE_EXECUTIONS.get(job_id)
        if current is None or _NATIVE_EXECUTIONS.get(job_id) is not current:
            return
        current.cleanup_requested = True
        # Until the process manager reports a terminal cleanup result, an
        # absent receipt is unknown. Never project it as ``not_requested``.
        if current.cleanup_status == "not_requested":
            current.cleanup_status = "unknown"
        current.cancel_event.set()


def _remember_native_process_cleanup(result: dict[str, Any]) -> dict[str, Any]:
    """Attach the latest process-manager cleanup evidence to the live job."""
    job_id = _NATIVE_JOB_ID.get()
    if not job_id:
        return result
    with _NATIVE_EXECUTION_LOCK:
        control = _NATIVE_EXECUTIONS.get(job_id)
        if control is None:
            return result
        status = str(result.get("cleanup_status") or "not_requested")
        if control.cleanup_requested and status == "not_requested":
            status = "unknown"
            result["cleanup_status"] = status
        control.cleanup_status = status
        control.remaining_descendants = result.get("remaining_descendants")
        control.worker_root = result.get("worker_root")
        if result.get("cancelled") or control.cleanup_requested:
            control.cleanup_requested = True
    return result


def _job_workspace(request: NativeSoftwareEngineeringRequest) -> _JobWorkspace:
    root = _workspace_root()
    token = _job_token(request.job_id)
    relative_root = Path(".seraph") / "native-software-engineering" / "jobs" / token
    job_root = root / relative_root
    workspace = job_root / "workspace"
    artifacts = job_root / "artifacts"
    branch = f"seraph-job-{token}"
    if workspace.exists() or artifacts.exists():
        raise NativeSoftwareEngineeringError("job_workspace_already_exists")
    try:
        workspace.relative_to(root)
        artifacts.relative_to(root)
    except ValueError as exc:
        raise NativeSoftwareEngineeringError("job_workspace_outside_workspace") from exc
    return _JobWorkspace(
        root=workspace,
        relative_root=relative_root.as_posix() + "/workspace",
        branch=branch,
        artifact_dir=artifacts,
        relative_artifact_dir=relative_root.as_posix() + "/artifacts",
    )


def _copy_fixture(prepared: _PreparedFixture, job_workspace: _JobWorkspace) -> None:
    try:
        job_workspace.root.relative_to(prepared.source)
    except ValueError:
        pass
    else:
        raise NativeSoftwareEngineeringError("job_workspace_inside_fixture")
    job_workspace.root.parent.mkdir(parents=True, exist_ok=False)
    shutil.copytree(
        prepared.source,
        job_workspace.root,
        symlinks=False,
        ignore=shutil.ignore_patterns(*sorted(_GENERATED_FIXTURE_DIRS)),
    )
    job_workspace.artifact_dir.mkdir(parents=True, exist_ok=False)


def _relative_workspace_path(path: Path) -> str:
    return _safe_relative(path, _workspace_root(), reason_code="workspace_path_outside_workspace")


def _safe_json(payload: Any) -> str:
    # Stream encoder chunks into a bounded buffer.  In particular, do not
    # create an unbounded ``json.dumps`` result before checking its size.
    encoder = json.JSONEncoder(ensure_ascii=True, sort_keys=True, indent=2).iterencode(payload)
    chunks: list[str] = []
    total_bytes = 0
    for chunk in encoder:
        total_bytes += len(chunk.encode("utf-8"))
        if total_bytes + 1 > _MAX_ARTIFACT_BYTES:
            raise NativeSoftwareEngineeringError("artifact_size_exceeded")
        chunks.append(chunk)
    return "".join(chunks) + "\n"


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    content = _safe_json(payload)
    root = _workspace_root()
    try:
        relative = path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError as exc:
        raise NativeSoftwareEngineeringError("artifact_path_outside_workspace") from exc
    try:
        resolved = _safe_resolve(relative)
        _write_workspace_text_bounded(
            resolved,
            content,
            max_bytes=_MAX_ARTIFACT_BYTES,
            create_parents=True,
        )
    except (OSError, ValueError) as exc:
        raise NativeSoftwareEngineeringError("artifact_write_blocked") from exc
    return content


def _execute_native_capability(
    capability_id: str,
    arguments: dict[str, Any],
    *,
    destination: str,
    approval_id: str = "",
    approval_digest: str = "",
    approval_binding: Mapping[str, Any] | None = None,
    _return_raw_result: bool = False,
) -> Any:
    """Route native SWE effects through the same fixed capability host."""
    principal = get_current_trust_principal()
    session_id = get_current_session_id()
    if principal is None or session_id is None:
        raise NativeSoftwareEngineeringError("runtime_principal_missing")
    request = build_capability_request(
        capability_id=capability_id,
        arguments=arguments,
        owner_principal_id=principal.principal_id,
        session_id=session_id,
        job_id=_NATIVE_JOB_ID.get() or principal.job_id,
        destination=destination,
        approval_id=approval_id,
        approval_digest=approval_digest,
        approval_binding=approval_binding,
    )
    try:
        host = current_capability_execution_host()
        if _return_raw_result:
            _, receipt = host._execute_adopted_internal_result(  # noqa: SLF001 - module-owned native seam
                request,
                _token=_RAW_RESULT_TOKEN,
            )
        else:
            receipt = host.execute(request)
    except CapabilityExecutionError as exc:
        raise NativeSoftwareEngineeringError(f"capability_{exc.reason_code}") from exc
    if receipt.state != "succeeded":
        raise NativeSoftwareEngineeringError(f"capability_{receipt.state}")
    return receipt.result


def _process_result(
    command: str,
    args: Iterable[str],
    cwd: str,
    *,
    timeout_seconds: int | None = None,
    include_output: bool = False,
) -> dict[str, Any]:
    args_list = list(args)
    cancel_event = _NATIVE_CANCEL_EVENT.get()
    try:
        result = _execute_native_capability(
            "run_command",
            {
                "command": command,
                "args_json": json.dumps(args_list),
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "cancel_event": cancel_event,
            },
            destination=f"workspace-process:{cwd or '.'}",
            _return_raw_result=True,
        )
    except ValueError as exc:
        return _remember_native_process_cleanup({
            "ok": False,
            "blocked": True,
            "cancelled": False,
            "reason_code": "native_command_policy_blocked",
            "exit_code": None,
            "timed_out": False,
            "stdout_sha256": _digest_text(""),
            "stderr_sha256": _digest_text(str(exc)),
            "stdout_chars": 0,
            "stderr_chars": len(str(exc)),
            "cleanup_status": "not_requested",
            "remaining_descendants": None,
            "worker_root": None,
        })
    except (OSError, RuntimeError):
        return _remember_native_process_cleanup({
            "ok": False,
            "blocked": False,
            "cancelled": False,
            "reason_code": "native_process_unavailable",
            "exit_code": None,
            "timed_out": False,
            "stdout_sha256": _digest_text(""),
            "stderr_sha256": _digest_text(""),
            "stdout_chars": 0,
            "stderr_chars": 0,
            "cleanup_status": "not_requested",
            "remaining_descendants": None,
            "worker_root": None,
        })
    payload = {
        "ok": bool(result.get("ok")),
        "blocked": False,
        "cancelled": bool(result.get("cancelled")),
        "reason_code": (
            "operator_cancelled_during_process"
            if result.get("cancelled")
            else "process_completed" if result.get("ok") else "process_failed"
        ),
        "exit_code": result.get("exit_code"),
        "timed_out": bool(result.get("timed_out")),
        "stdout_sha256": _digest_text(str(result.get("stdout") or "")),
        "stderr_sha256": _digest_text(str(result.get("stderr") or "")),
        "stdout_chars": len(str(result.get("stdout") or "")),
        "stderr_chars": len(str(result.get("stderr") or "")),
        "cleanup_status": result.get("cleanup_status", "not_requested"),
        "remaining_descendants": result.get("remaining_descendants"),
        "worker_root": result.get("worker_root"),
    }
    if include_output:
        payload["_stdout"] = str(result.get("stdout") or "")
        payload["_stderr"] = str(result.get("stderr") or "")
    return _remember_native_process_cleanup(payload)


def _workspace_receipt(job_workspace: _JobWorkspace) -> dict[str, Any]:
    return {
        "relative_path": job_workspace.relative_root,
        "branch": job_workspace.branch,
        "recoverable": True,
        "artifact_relative_path": job_workspace.relative_artifact_dir,
        "owner_scope": "durable_job",
    }


def _process_cleanup_receipt(
    result: dict[str, Any] | None,
    *,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Carry bounded process-cleanup truth into higher-level workflow receipts."""
    if result is not None:
        control = _native_execution_for_job(str(job_id or "")) if job_id else None
        cleanup_status = str(result.get("cleanup_status") or "not_requested")
        if control is not None and control.cleanup_requested and cleanup_status == "not_requested":
            # A cancellation request may race a wrapper that returned an
            # incomplete result. Preserve the live cancellation evidence
            # instead of publishing the misleading default state.
            cleanup_status = control.cleanup_status if control.cleanup_status != "not_requested" else "unknown"
        return {
            "cleanup_status": cleanup_status,
            "remaining_descendants": (
                result.get("remaining_descendants")
                if result.get("remaining_descendants") is not None or control is None
                else control.remaining_descendants
            ),
            "worker_root": (
                result.get("worker_root")
                if result.get("worker_root") is not None or control is None
                else control.worker_root
            ),
        }
    control = _native_execution_for_job(str(job_id or "")) if job_id else None
    if control is None:
        return {
            "cleanup_status": "unknown",
            "remaining_descendants": None,
            "worker_root": None,
            "cleanup_requested": True,
        }
    return {
        "cleanup_status": (
            control.cleanup_status
            if control.cleanup_status != "not_requested"
            else "unknown"
        ),
        "remaining_descendants": control.remaining_descendants,
        "worker_root": control.worker_root,
        "cleanup_requested": control.cleanup_requested,
    }


def _changed_workspace_paths(status_output: str) -> set[str]:
    """Return meaningful fixture changes while ignoring disposable test caches."""
    changed_paths: set[str] = set()
    for line in status_output.splitlines():
        if len(line) < 4 or "->" in line:
            continue
        path = line[3:].strip()
        if not path:
            continue
        path_parts = Path(path.rstrip("/")).parts
        if any(part in _GENERATED_FIXTURE_DIRS for part in path_parts):
            continue
        changed_paths.add(path)
    return changed_paths


async def _record_artifact(
    job_id: str,
    job_workspace: _JobWorkspace,
    *,
    filename: str,
    artifact_type: str,
    payload: dict[str, Any],
    owner: str,
    fencing_token: int,
) -> dict[str, Any]:
    path = job_workspace.artifact_dir / filename
    content = _write_json(path, payload)
    relative_path = _relative_workspace_path(path)
    return await durable_job_repository.record_artifact(
        job_id,
        file_path=relative_path,
        artifact_type=artifact_type,
        content=content,
        owner=owner,
        fencing_token=fencing_token,
    )


async def _record_checkpoint(
    job_id: str,
    phase: str,
    *,
    artifact_path: str | None,
    owner: str,
    fencing_token: int,
) -> dict[str, Any]:
    return await durable_job_repository.record_checkpoint(
        job_id,
        checkpoint_id=f"native-swe:{phase}",
        state={"phase": phase, "artifact_path": artifact_path, "recoverable": True},
        owner=owner,
        fencing_token=fencing_token,
    )


async def _fail_claimed_job(
    job_id: str,
    *,
    owner: str,
    fencing_token: int,
    reason_code: str,
    blocked: bool = False,
) -> dict[str, Any] | None:
    try:
        return await durable_job_repository.transition_job(
            job_id,
            "blocked" if blocked else "failed",
            owner=owner,
            fencing_token=fencing_token,
            reason=reason_code,
            result_summary="native fixture execution blocked" if blocked else "native fixture execution failed",
        )
    except Exception:
        return await durable_job_repository.get_job(job_id)


async def _cancel_claimed_job(
    job_id: str,
    *,
    owner: str,
    fencing_token: int,
    reason: str,
    expected_revision: int | None = None,
) -> dict[str, Any] | None:
    try:
        return await durable_job_repository.cancel_job(
            job_id,
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=expected_revision,
            reason=reason,
        )
    except Exception:
        return await durable_job_repository.get_job(job_id)


def _cancellation_result(
    request: NativeSoftwareEngineeringRequest,
    prepared: _PreparedFixture,
    job_workspace: _JobWorkspace,
    durable_job: dict[str, Any] | None,
    *,
    reason_code: str,
    test_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a recovery receipt that can never be mistaken for success."""
    cancellation_payload = {
        "schema_version": "seraph.native-software-engineering.cancellation.v1",
        "job_id": request.job_id,
        "reason_code": reason_code,
        "phase": "test" if test_result is not None else "apply",
        "test": test_result or None,
        "process_cleanup": _process_cleanup_receipt(test_result, job_id=request.job_id),
        "success_eligible": False,
        "workspace_recoverable": True,
    }
    # A cancellation can race the durable terminal transition. The local file
    # is best-effort recovery evidence; the durable transition receipt remains
    # the authoritative cancellation record.
    try:
        _write_json(job_workspace.artifact_dir / "cancellation.json", cancellation_payload)
    except OSError:
        cancellation_payload["artifact_write_failed"] = True
    durable_status = str((durable_job or {}).get("status") or "cancelled")
    return {
        "status": durable_status,
        "reason_code": reason_code,
        "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
        "provider": None,
        "workspace": _workspace_receipt(job_workspace),
        "durable_job": durable_job,
        "cancellation": cancellation_payload,
        "process_cleanup": _process_cleanup_receipt(test_result, job_id=request.job_id),
        "original_fixture_immutable": _fixture_tree_digest(prepared.source) == prepared.source_digest,
        "operator_visible": True,
    }


async def _fail_unclaimed_job(request: NativeSoftwareEngineeringRequest, reason_code: str) -> dict[str, Any] | None:
    """Record a queue/claim failure while the service owner fence is still durable."""
    try:
        return await durable_job_repository.fail_unclaimed_job(
            request.job_id,
            owner_principal_id=request.owner_principal_id,
            service_id=str(request.service_id or ""),
            reason=reason_code,
            result_summary="native fixture could not acquire its durable execution lease",
        )
    except Exception:
        # Never replace the original queue/claim failure with an ownerless write.
        return await durable_job_repository.get_job(request.job_id)


async def _record_test_failure_evidence(
    request: NativeSoftwareEngineeringRequest,
    prepared: _PreparedFixture,
    job_workspace: _JobWorkspace,
    *,
    worker_owner: str,
    fencing_token: int,
    test_result: dict[str, Any],
) -> bool:
    """Persist diagnosis and readback even when the test process fails."""
    diff_check = _process_result("git", ["diff", "--check"], job_workspace.relative_root)
    status_process = _process_result(
        "git", ["status", "--short"], job_workspace.relative_root, include_output=True
    )
    status_output = str(status_process.pop("_stdout", ""))
    changed_paths = _changed_workspace_paths(status_output)
    patched_path = job_workspace.root / FIXTURE_BUG_FILE
    patched_body = patched_path.read_text(encoding="utf-8") if patched_path.is_file() else ""
    source_immutable = _fixture_tree_digest(prepared.source) == prepared.source_digest
    exact_scope = changed_paths == {FIXTURE_BUG_FILE}
    readback_ok = bool(
        diff_check["ok"]
        and status_process["ok"]
        and exact_scope
        and FIXTURE_AFTER_TEXT in patched_body
        and FIXTURE_BEFORE_TEXT not in patched_body
        and source_immutable
    )
    diagnose_payload = {
        "schema_version": "seraph.native-software-engineering.diagnose.v1",
        "job_id": request.job_id,
        "failed_phase": "test",
        "test_reason": "test_timeout" if test_result.get("timed_out") else "test_process_failed",
        "test_exit_code": test_result.get("exit_code"),
        "test_process": test_result,
        "process_cleanup": _process_cleanup_receipt(test_result),
        "git_diff_check": diff_check,
        "git_status": status_process,
        "changed_paths": sorted(changed_paths),
        "exact_workspace_scope": exact_scope,
        "original_fixture_immutable": source_immutable,
        "operator_visible": True,
    }
    await _record_artifact(
        request.job_id,
        job_workspace,
        filename="diagnose.json",
        artifact_type="native_swe_diagnose",
        payload=diagnose_payload,
        owner=worker_owner,
        fencing_token=fencing_token,
    )
    readback_payload = {
        "schema_version": "seraph.native-software-engineering.readback.v1",
        "job_id": request.job_id,
        "file_path": FIXTURE_BUG_FILE,
        "patched_sha256": _digest_text(patched_body),
        "expected_after_sha256": _digest_text(FIXTURE_AFTER_TEXT),
        "old_text_absent": FIXTURE_BEFORE_TEXT not in patched_body,
        "new_text_present": FIXTURE_AFTER_TEXT in patched_body,
        "original_fixture_immutable": source_immutable,
        "exact_workspace_scope": exact_scope,
        "processes": {
            "test": test_result,
            "git_diff_check": diff_check,
            "git_status": status_process,
        },
        "verified": readback_ok,
        "test_success": False,
    }
    await _record_artifact(
        request.job_id,
        job_workspace,
        filename="readback.json",
        artifact_type="native_swe_readback",
        payload=readback_payload,
        owner=worker_owner,
        fencing_token=fencing_token,
    )
    await durable_job_repository.record_readback(
        request.job_id,
        target_path=_relative_workspace_path(patched_path),
        status="succeeded" if readback_ok else "failed",
        target_digest=_digest_text(patched_body),
        content_sha256=_digest_text(patched_body),
        details={
            "diagnose_artifact": _relative_workspace_path(job_workspace.artifact_dir / "diagnose.json"),
            "readback_artifact": _relative_workspace_path(job_workspace.artifact_dir / "readback.json"),
            "test_exit_code": test_result.get("exit_code"),
            "process_cleanup": _process_cleanup_receipt(test_result),
            "exact_workspace_scope": exact_scope,
            "original_fixture_immutable": source_immutable,
            "verified": readback_ok,
        },
        owner=worker_owner,
        fencing_token=fencing_token,
    )
    await _record_checkpoint(
        request.job_id,
        "diagnose",
        artifact_path=_relative_workspace_path(job_workspace.artifact_dir / "diagnose.json"),
        owner=worker_owner,
        fencing_token=fencing_token,
    )
    await _record_checkpoint(
        request.job_id,
        "readback",
        artifact_path=_relative_workspace_path(job_workspace.artifact_dir / "readback.json"),
        owner=worker_owner,
        fencing_token=fencing_token,
    )
    return readback_ok


async def run_native_software_engineering_fixture(
    request: NativeSoftwareEngineeringRequest | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Run inspect -> plan -> patch -> test -> readback for a bounded fixture.

    Expected policy rejections return an operator-safe ``blocked`` receipt.  A
    failed or timed-out test returns ``failed`` and keeps the job workspace for
    recovery.  The original fixture is never used as a patch target.
    """
    request = _request_from_inputs(request, overrides)
    preflight = preflight_native_software_engineering_fixture(request)
    if preflight["status"] != "ready":
        return preflight
    runtime_principal = get_current_trust_principal()
    if runtime_principal is None:
        return {
            "status": "blocked",
            "reason_code": "runtime_principal_missing",
            "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
            "provider": None,
            "operator_visible": True,
        }
    try:
        prepared = _prepare_fixture(request, executable=True)
    except NativeSoftwareEngineeringError as exc:
        return {
            "status": "blocked",
            "reason_code": exc.reason_code,
            "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
            "provider": None,
            "operator_visible": True,
        }
    if prepared.source_digest != preflight.get("source_digest"):
        return {
            "status": "blocked",
            "reason_code": "fixture_changed_after_preflight",
            "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
            "provider": None,
            "operator_visible": True,
        }
    inspection = preflight
    plan = build_native_software_engineering_plan(request, inspection)
    declared_authority = {
        "principal": request.owner_principal_id,
        "owner_kind": request.owner_kind,
        "service_id": request.service_id,
        "capability_id": NATIVE_SOFTWARE_ENGINEERING_CAPABILITY_ID,
        "capability_version": NATIVE_SOFTWARE_ENGINEERING_CAPABILITY_VERSION,
        "approval_id": request.approval_id,
        "attempt_id": _native_attempt_id(request),
        "actor_roles_are_metadata_only": True,
    }
    identity = DurableJobIdentity(
        job_id=request.job_id,
        owner_kind=request.owner_kind,
        owner_principal_id=request.owner_principal_id,
        job_kind=NATIVE_SOFTWARE_ENGINEERING_JOB_KIND,
        capability_version=NATIVE_SOFTWARE_ENGINEERING_CAPABILITY_VERSION,
        idempotency_scope=NATIVE_SOFTWARE_ENGINEERING_IDEMPOTENCY_SCOPE,
        idempotency_key=request.idempotency_key or request.job_id,
    )
    spec = DurableJobSpec(
        identity=identity,
        inputs={
            "fixture": NATIVE_SOFTWARE_ENGINEERING_CAPABILITY_ID,
            "bug_file": FIXTURE_BUG_FILE,
            "test_file": FIXTURE_TEST_FILE,
            "source_digest": prepared.source_digest,
        },
        session_id=request.session_id,
        priority=request.priority,
        resource_claims=("cpu", "workspace"),
        declared_authority=declared_authority,
        deadline_at=request.deadline_at,
        max_attempts=request.max_attempts,
        service_id=request.service_id,
    )
    try:
        admitted = await durable_job_repository.admit_job(spec)
    except Exception:
        return {
            "status": "blocked",
            "reason_code": "job_admission_blocked",
            "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
            "provider": None,
            "operator_visible": True,
        }
    if admitted.get("receipt", {}).get("status") == "deduped":
        return {
            "status": admitted.get("status", "blocked"),
            "reason_code": "job_idempotency_deduped",
            "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
            "provider": None,
            "durable_job": admitted,
            "operator_visible": True,
        }
    if admitted.get("status") != "accepted":
        return {
            "status": admitted.get("status", "failed"),
            "reason_code": admitted.get("failure_reason") or "job_not_accepted",
            "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
            "provider": None,
            "durable_job": admitted,
            "operator_visible": True,
        }

    job_workspace: _JobWorkspace | None = None
    worker_owner = f"native-swe-worker:{_job_token(request.job_id)}"
    fencing_token: int | None = None
    durable_job: dict[str, Any] | None = admitted
    execution_control: _NativeExecutionControl | None = None
    cancel_context_token: Any = None
    job_context_token: Any = None
    fencing_context_token: Any = None
    runtime_principal_token: Any = None
    runtime_fencing_token: Any = None
    try:
        try:
            await durable_job_repository.queue_job(request.job_id)
        except Exception:
            durable_job = await _fail_unclaimed_job(request, "job_queue_failed")
            return {
                "status": "failed",
                "reason_code": "job_queue_failed",
                "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
                "provider": None,
                "durable_job": durable_job,
                "operator_visible": True,
            }
        try:
            claimed = await durable_job_repository.claim_job(request.job_id, owner=worker_owner, lease_seconds=300)
        except Exception:
            durable_job = await _fail_unclaimed_job(request, "job_claim_failed")
            return {
                "status": "failed",
                "reason_code": "job_claim_failed",
                "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
                "provider": None,
                "durable_job": durable_job,
                "operator_visible": True,
            }
        durable_job = claimed
        fencing_token = int(claimed["lease"]["fencing_token"])
        job_context_token = _NATIVE_JOB_ID.set(request.job_id)
        fencing_context_token = _NATIVE_FENCING_TOKEN.set(str(fencing_token))
        runtime_principal_token = set_runtime_trust_principal(
            replace(runtime_principal, job_id=request.job_id)
        )
        runtime_fencing_token = set_runtime_fencing_token(str(fencing_token))
        execution_control = _NativeExecutionControl(
            owner=worker_owner,
            fencing_token=fencing_token,
            cancel_event=threading.Event(),
        )
        _register_native_execution(request.job_id, execution_control)
        cancel_context_token = _NATIVE_CANCEL_EVENT.set(execution_control.cancel_event)
        job_workspace = _job_workspace(request)
        _copy_fixture(prepared, job_workspace)
        relative_bug_path = _relative_workspace_path(job_workspace.root / FIXTURE_BUG_FILE)

        # The setup itself is process-backed so branch identity and baseline
        # commit are independently observable through the same native runtime.
        init = _process_result("git", ["init", "--initial-branch", job_workspace.branch], job_workspace.relative_root)
        if not init["ok"]:
            raise NativeSoftwareEngineeringError("workspace_git_init_failed")
        add = _process_result("git", ["add", "."], job_workspace.relative_root)
        commit = _process_result(
            "git",
            [
                "-c",
                "user.name=Seraph Fixture",
                "-c",
                "user.email=seraph-fixture@example.invalid",
                "commit",
                "-m",
                "fixture baseline",
            ],
            job_workspace.relative_root,
        )
        branch = _process_result("git", ["branch", "--show-current"], job_workspace.relative_root)
        if not add["ok"] or not commit["ok"] or not branch["ok"]:
            raise NativeSoftwareEngineeringError("workspace_baseline_failed")

        inspect_payload = {
            **inspection,
            "job_id": request.job_id,
            "workspace": _workspace_receipt(job_workspace),
            "branch_setup": {"init": init, "add": add, "commit": commit, "branch": branch},
            "original_fixture_immutable": True,
        }
        await _record_artifact(
            request.job_id,
            job_workspace,
            filename="inspect.json",
            artifact_type="native_swe_inspection",
            payload=inspect_payload,
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        await _record_checkpoint(
            request.job_id,
            "inspect",
            artifact_path=_relative_workspace_path(job_workspace.artifact_dir / "inspect.json"),
            owner=worker_owner,
            fencing_token=fencing_token,
        )

        plan_payload = {
            **plan,
            "workspace": _workspace_receipt(job_workspace),
            "bug_target": relative_bug_path,
        }
        await _record_artifact(
            request.job_id,
            job_workspace,
            filename="plan.json",
            artifact_type="native_swe_plan",
            payload=plan_payload,
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        await _record_checkpoint(
            request.job_id,
            "plan",
            artifact_path=_relative_workspace_path(job_workspace.artifact_dir / "plan.json"),
            owner=worker_owner,
            fencing_token=fencing_token,
        )

        preview_raw = _execute_native_capability(
            "preview_workspace_patch",
            {
                "file_path": relative_bug_path,
                "old_text": FIXTURE_BEFORE_TEXT,
                "new_text": FIXTURE_AFTER_TEXT,
                "expected_occurrences": 1,
            },
            destination=f"workspace:{relative_bug_path}",
        )
        preview_payload = json.loads(preview_raw)
        if not isinstance(preview_payload, dict) or preview_payload.get("applied"):
            raise NativeSoftwareEngineeringError("patch_preview_invalid")
        patch_arguments = _native_patch_arguments(
            relative_bug_path,
            preview_payload=preview_payload,
        )
        approval_context = _native_approval_context(
            request,
            prepared,
            relative_bug_path=relative_bug_path,
            preview_payload=preview_payload,
        )
        approval_fingerprint = _native_approval_fingerprint(approval_context)
        approval_operator_session: str | None = None
        approval_binding: dict[str, Any] | None = None
        pending_approval_id: str | None = None
        if request.patch_approval in {"required", "approved"}:
            approval_operator_session = _native_approval_owner_session(
                runtime_principal,
                session_id=request.session_id,
            )
        if request.patch_approval == "required":
            approval_expires_at = time.time() + _NATIVE_APPROVAL_TTL_SECONDS
            try:
                pending_request = await approval_repository.get_or_create_pending(
                    session_id=request.session_id,
                    tool_name=_NATIVE_PATCH_CAPABILITY_ID,
                    risk_level=_NATIVE_APPROVAL_RISK,
                    summary=(
                        f"Approve bounded native SWE patch for {FIXTURE_BUG_FILE} "
                        f"in job {request.job_id}"
                    ),
                    fingerprint=approval_fingerprint,
                    details={
                        "approval_conversation_id": request.session_id,
                        "approval_owner_operator_session_id": approval_operator_session,
                        "approval_context": approval_context,
                        "approval_expires_at": approval_expires_at,
                        "expires_at": approval_expires_at,
                        "action": "apply",
                        "capability_id": _NATIVE_PATCH_CAPABILITY_ID,
                    },
                )
            except Exception as exc:
                raise NativeSoftwareEngineeringError("approval_request_creation_failed") from exc
            pending_approval_id = str(getattr(pending_request, "id", "") or "") or None
        elif request.patch_approval == "approved":
            try:
                consumed = await approval_repository.consume_approved(
                    session_id=request.session_id,
                    tool_name=_NATIVE_PATCH_CAPABILITY_ID,
                    fingerprint=approval_fingerprint,
                    owner_operator_session_id=approval_operator_session,
                    approval_id=request.approval_id,
                )
            except Exception as exc:
                raise NativeSoftwareEngineeringError("approval_request_consume_failed") from exc
            if not isinstance(consumed, dict):
                raise NativeSoftwareEngineeringError("approval_not_current")
            approval_binding = consumed
            if not verify_capability_approval(approval_binding):
                raise NativeSoftwareEngineeringError("approval_binding_invalid")
            binding_context = approval_binding.get("approval_context")
            if (
                str(approval_binding.get("approval_id") or "") != str(request.approval_id or "")
                or str(approval_binding.get("status") or "") != "consumed"
                or str(approval_binding.get("session_id") or "") != request.session_id
                or str(approval_binding.get("tool_name") or "") != _NATIVE_PATCH_CAPABILITY_ID
                or str(approval_binding.get("fingerprint") or "") != approval_fingerprint
                or str(approval_binding.get("owner_operator_session_id") or "") != approval_operator_session
                or not isinstance(binding_context, Mapping)
                or dict(binding_context) != approval_context
            ):
                raise NativeSoftwareEngineeringError("approval_binding_mismatch")
        await _record_artifact(
            request.job_id,
            job_workspace,
            filename="patch.preview.json",
            artifact_type="native_swe_patch_preview",
            payload=preview_payload,
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        approval_payload = {
            "schema_version": "seraph.native-software-engineering.approval.v1",
            "job_id": request.job_id,
            "state": request.patch_approval,
            "required": True,
            "source": (
                "repository_approval_binding"
                if request.patch_approval == "approved"
                else "approval_request_repository"
            ),
            "authority_source": "authenticated_operator_approval",
            "owner_principal_id": request.owner_principal_id,
            "actor_roles_are_metadata_only": True,
            "approval_id": (
                str(approval_binding.get("approval_id"))
                if approval_binding is not None
                else pending_approval_id
            ),
            "approval_fingerprint": approval_fingerprint,
            "approval_context_digest": _digest(approval_context),
            "scope": {
                "file_path": FIXTURE_BUG_FILE,
                "before_sha256": preview_payload["before_sha256"],
                "after_sha256": preview_payload["after_sha256"],
                "preview_artifact": _relative_workspace_path(job_workspace.artifact_dir / "patch.preview.json"),
                "preview_digest": _digest(approval_context["patch"]),
            },
        }
        await _record_artifact(
            request.job_id,
            job_workspace,
            filename="approval.json",
            artifact_type="native_swe_patch_approval",
            payload=approval_payload,
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        await durable_job_repository.record_effect(
            request.job_id,
            effect_type="workspace_patch_approval",
            target_path=relative_bug_path,
            status="succeeded" if request.patch_approval == "approved" else "blocked",
            details={
                "approval_artifact": _relative_workspace_path(job_workspace.artifact_dir / "approval.json"),
                "state": request.patch_approval,
                "source": (
                    "repository_approval_binding"
                    if request.patch_approval == "approved"
                    else "approval_request_repository"
                ),
                "approval_id": (
                    str(approval_binding.get("approval_id"))
                    if approval_binding is not None
                    else pending_approval_id
                ),
                "approval_fingerprint": approval_fingerprint,
                "approval_context_digest": _digest(approval_context),
            },
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        await _record_checkpoint(
            request.job_id,
            "approval",
            artifact_path=_relative_workspace_path(job_workspace.artifact_dir / "approval.json"),
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        if request.patch_approval != "approved":
            if request.patch_approval == "required":
                durable_job = await durable_job_repository.transition_job(
                    request.job_id,
                    "awaiting_approval",
                    owner=worker_owner,
                    fencing_token=fencing_token,
                    reason="patch_approval_required",
                    result_summary="workspace patch awaits operator approval",
                )
                return {
                    "status": "awaiting_approval",
                    "reason_code": "patch_approval_required",
                    "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
                    "provider": None,
                    "workspace": _workspace_receipt(job_workspace),
                    "durable_job": durable_job,
                    "approval_id": pending_approval_id,
                    "approval_fingerprint": approval_fingerprint,
                    "original_fixture_immutable": _fixture_tree_digest(prepared.source) == prepared.source_digest,
                    "operator_visible": True,
                }
            durable_job = await _fail_claimed_job(
                request.job_id,
                owner=worker_owner,
                fencing_token=fencing_token,
                reason_code="patch_approval_denied",
                blocked=True,
            )
            return {
                "status": "blocked",
                "reason_code": "patch_approval_denied",
                "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
                "provider": None,
                "workspace": _workspace_receipt(job_workspace),
                "durable_job": durable_job,
                "original_fixture_immutable": _fixture_tree_digest(prepared.source) == prepared.source_digest,
                "operator_visible": True,
            }
        if execution_control is not None and execution_control.cancel_event.is_set():
            durable_job = await _cancel_claimed_job(
                request.job_id,
                owner=worker_owner,
                fencing_token=fencing_token,
                reason="operator_cancelled_before_apply",
            )
            return _cancellation_result(
                request,
                prepared,
                job_workspace,
                durable_job,
                reason_code="operator_cancelled_before_apply",
            )
        patch_target_digest = str(preview_payload.get("after_sha256") or "")
        patch_effect_id = "workspace_patch:" + _digest(
            {
                "job_id": request.job_id,
                "target_path": relative_bug_path,
                "target_digest": patch_target_digest,
            }
        )[:24]
        patch_approval_id = (
            str(approval_binding.get("approval_id"))
            if approval_binding is not None
            else None
        )
        patch_adapter_key = f"native-swe:workspace-patch:{request.job_id}:{patch_target_digest}"
        # Establish the intended external write before applying it. If the
        # process dies after this point, restart recovery keeps the effect
        # uncertain until a readback/reconciliation receipt resolves it.
        intent = await durable_job_repository.record_effect(
            request.job_id,
            effect_id=patch_effect_id,
            effect_type="workspace_patch",
            target_path=relative_bug_path,
            target_digest=patch_target_digest,
            approval_id=patch_approval_id,
            adapter_idempotency_key=patch_adapter_key,
            status="intent",
            details={
                "preview_artifact": _relative_workspace_path(job_workspace.artifact_dir / "patch.preview.json"),
                "approval_artifact": _relative_workspace_path(job_workspace.artifact_dir / "approval.json"),
            },
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        dispatch_kwargs = {
            "effect_id": patch_effect_id,
            "effect_type": "workspace_patch",
            "target_path": relative_bug_path,
            "target_digest": patch_target_digest,
            "approval_id": patch_approval_id,
            "adapter_idempotency_key": patch_adapter_key,
            "status": "dispatched",
            "details": {
                "preview_artifact": _relative_workspace_path(job_workspace.artifact_dir / "patch.preview.json"),
                "approval_artifact": _relative_workspace_path(job_workspace.artifact_dir / "approval.json"),
            },
            "owner": worker_owner,
            "fencing_token": fencing_token,
        }
        intent_revision = intent.get("revision") if isinstance(intent, dict) else None
        if execution_control is not None and execution_control.cancel_event.is_set():
            durable_job = await _cancel_claimed_job(
                request.job_id,
                owner=worker_owner,
                fencing_token=fencing_token,
                expected_revision=(int(intent_revision) if intent_revision is not None else None),
                reason="operator_cancelled_before_dispatch",
            )
            return _cancellation_result(
                request,
                prepared,
                job_workspace,
                durable_job,
                reason_code="operator_cancelled_before_dispatch",
            )
        if intent_revision is not None:
            dispatch_kwargs["expected_revision"] = int(intent_revision)
        dispatched = await durable_job_repository.record_effect(
            request.job_id,
            **dispatch_kwargs,
        )
        if execution_control is not None and execution_control.cancel_event.is_set():
            dispatched_revision = (
                dispatched.get("revision") if isinstance(dispatched, dict) else None
            )
            durable_job = await _cancel_claimed_job(
                request.job_id,
                owner=worker_owner,
                fencing_token=fencing_token,
                expected_revision=(
                    int(dispatched_revision) if dispatched_revision is not None else None
                ),
                reason="operator_cancelled_before_apply",
            )
            return _cancellation_result(
                request,
                prepared,
                job_workspace,
                durable_job,
                reason_code="operator_cancelled_before_apply",
            )
        # Reacquire the effect boundary immediately before touching the
        # workspace.  This conditional revision/fence write closes the
        # cancellation race: cancellation wins the CAS and no patch runs; if
        # this write wins, a later cancellation leaves the dispatched effect
        # unresolved for restart reconciliation.
        dispatched_revision = (
            dispatched.get("revision") if isinstance(dispatched, dict) else None
        )
        boundary_kwargs = {
            "effect_id": patch_effect_id,
            "effect_type": "workspace_patch",
            "target_path": relative_bug_path,
            "target_digest": patch_target_digest,
            "approval_id": patch_approval_id,
            "adapter_idempotency_key": patch_adapter_key,
            "status": "dispatched",
            "details": {
                "preview_artifact": _relative_workspace_path(job_workspace.artifact_dir / "patch.preview.json"),
                "approval_artifact": _relative_workspace_path(job_workspace.artifact_dir / "approval.json"),
                "effect_boundary": "apply_workspace_patch",
            },
            "owner": worker_owner,
            "fencing_token": fencing_token,
        }
        if dispatched_revision is not None:
            boundary_kwargs["expected_revision"] = int(dispatched_revision)
        await durable_job_repository.record_effect(
            request.job_id,
            **boundary_kwargs,
        )
        applied_raw = _execute_native_capability(
            "apply_workspace_patch",
            patch_arguments,
            destination=f"workspace:{relative_bug_path}",
            approval_id=patch_approval_id or "",
            approval_digest=approval_fingerprint if approval_binding is not None else "",
            approval_binding=approval_binding,
        )
        applied_payload = json.loads(applied_raw)
        if not isinstance(applied_payload, dict) or applied_payload.get("applied") is not True:
            raise NativeSoftwareEngineeringError("patch_application_invalid")
        patch_payload = {
            "schema_version": "seraph.native-software-engineering.patch.v1",
            "job_id": request.job_id,
            "file_path": FIXTURE_BUG_FILE,
            "before_sha256": applied_payload.get("before_sha256"),
            "after_sha256": applied_payload.get("after_sha256"),
            "diff_sha256": _digest_text(str(applied_payload.get("diff") or "")),
            "rollback": applied_payload.get("rollback"),
            "applied": True,
        }
        patch_artifact_receipt = await _record_artifact(
            request.job_id,
            job_workspace,
            filename="patch.json",
            artifact_type="native_swe_patch",
            payload=patch_payload,
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        patch_readback_kwargs = {
            "effect_id": patch_effect_id,
            "effect_type": "workspace_patch",
            "target_path": relative_bug_path,
            "target_digest": patch_target_digest,
            "status": "succeeded",
            "content_sha256": str(applied_payload.get("after_sha256") or ""),
            "details": {
                "patch_artifact": _relative_workspace_path(job_workspace.artifact_dir / "patch.json"),
                "verified": True,
            },
            "owner": worker_owner,
            "fencing_token": fencing_token,
        }
        patch_revision = patch_artifact_receipt.get("revision") if isinstance(patch_artifact_receipt, dict) else None
        if patch_revision is not None:
            patch_readback_kwargs["expected_revision"] = int(patch_revision)
        await durable_job_repository.record_readback(
            request.job_id,
            **patch_readback_kwargs,
        )
        await _record_checkpoint(
            request.job_id,
            "patch",
            artifact_path=_relative_workspace_path(job_workspace.artifact_dir / "patch.json"),
            owner=worker_owner,
            fencing_token=fencing_token,
        )

        if request.cancel_before_test:
            _mark_native_cleanup_requested(request.job_id, execution_control)
            durable_job = await _cancel_claimed_job(
                request.job_id,
                owner=worker_owner,
                fencing_token=fencing_token,
                reason="operator_cancelled_before_test",
            )
            return _cancellation_result(
                request,
                prepared,
                job_workspace,
                durable_job,
                reason_code="operator_cancelled_before_test",
            )

        try:
            test_result = await asyncio.wait_for(
                asyncio.to_thread(
                    _process_result,
                    request.test_command,
                    request.test_args,
                    job_workspace.relative_root,
                    timeout_seconds=request.test_timeout_seconds,
                ),
                timeout=request.test_timeout_seconds + 2,
            )
        except asyncio.TimeoutError:
            # The native runtime kills the process group at its own deadline;
            # this outer bound prevents a blocked worker from holding the job.
            test_result = {
                "ok": False,
                "blocked": False,
                "reason_code": "native_process_deadline_exceeded",
                "exit_code": None,
                "timed_out": True,
                "stdout_sha256": _digest_text(""),
                "stderr_sha256": _digest_text(""),
                "stdout_chars": 0,
                "stderr_chars": 0,
                "cleanup_status": "unknown",
                "remaining_descendants": None,
                "worker_root": None,
            }
        if (
            execution_control is not None
            and (execution_control.cancel_event.is_set() or test_result.get("cancelled"))
        ):
            durable_job = await _cancel_claimed_job(
                request.job_id,
                owner=worker_owner,
                fencing_token=fencing_token,
                reason="operator_cancelled_during_test",
            )
            return _cancellation_result(
                request,
                prepared,
                job_workspace,
                durable_job,
                reason_code="operator_cancelled_during_test",
                test_result=test_result,
            )
        test_payload = {
            "schema_version": "seraph.native-software-engineering.test.v1",
            "job_id": request.job_id,
            "command": request.test_command,
            "argument_count": len(request.test_args),
            "timeout_seconds": request.test_timeout_seconds,
            "process": test_result,
            "success_eligible": bool(test_result["ok"] and not test_result["timed_out"]),
        }
        await _record_artifact(
            request.job_id,
            job_workspace,
            filename="test.json",
            artifact_type="native_swe_test",
            payload=test_payload,
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        await durable_job_repository.record_effect(
            request.job_id,
            effect_type="test_process",
            target_path=_relative_workspace_path(job_workspace.root / FIXTURE_TEST_FILE),
            status="succeeded" if test_result["ok"] and not test_result["timed_out"] else "failed",
            details={
                "exit_code": test_result["exit_code"],
                "timed_out": test_result["timed_out"],
                "test_artifact": _relative_workspace_path(job_workspace.artifact_dir / "test.json"),
            },
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        await _record_checkpoint(
            request.job_id,
            "test",
            artifact_path=_relative_workspace_path(job_workspace.artifact_dir / "test.json"),
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        if test_result["blocked"]:
            durable_job = await _fail_claimed_job(
                request.job_id,
                owner=worker_owner,
                fencing_token=fencing_token,
                reason_code="native_command_policy_blocked",
                blocked=True,
            )
            return {
                "status": "blocked",
                "reason_code": "native_command_policy_blocked",
                "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
                "provider": None,
                "workspace": _workspace_receipt(job_workspace),
                "durable_job": durable_job,
                "original_fixture_immutable": _fixture_tree_digest(prepared.source) == prepared.source_digest,
                "operator_visible": True,
            }
        if not test_result["ok"] or test_result["timed_out"]:
            reason_code = "test_timeout" if test_result["timed_out"] else "test_process_failed"
            await _record_test_failure_evidence(
                request,
                prepared,
                job_workspace,
                worker_owner=worker_owner,
                fencing_token=fencing_token,
                test_result=test_result,
            )
            durable_job = await _fail_claimed_job(
                request.job_id,
                owner=worker_owner,
                fencing_token=fencing_token,
                reason_code=reason_code,
            )
            return {
                "status": "failed",
                "reason_code": reason_code,
                "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
                "provider": None,
                "workspace": _workspace_receipt(job_workspace),
                "durable_job": durable_job,
                "process_cleanup": _process_cleanup_receipt(test_result),
                "original_fixture_immutable": _fixture_tree_digest(prepared.source) == prepared.source_digest,
                "operator_visible": True,
            }

        readback_process = _process_result("git", ["diff", "--check"], job_workspace.relative_root)
        status_process = _process_result(
            "git", ["status", "--short"], job_workspace.relative_root, include_output=True
        )
        status_output = str(status_process.pop("_stdout", ""))
        changed_paths = _changed_workspace_paths(status_output)
        patched_body = (job_workspace.root / FIXTURE_BUG_FILE).read_text(encoding="utf-8")
        source_immutable = _fixture_tree_digest(prepared.source) == prepared.source_digest
        exact_scope = changed_paths == {FIXTURE_BUG_FILE}
        readback_ok = (
            readback_process["ok"]
            and status_process["ok"]
            and exact_scope
            and FIXTURE_AFTER_TEXT in patched_body
            and FIXTURE_BEFORE_TEXT not in patched_body
            and source_immutable
        )
        readback_payload = {
            "schema_version": "seraph.native-software-engineering.readback.v1",
            "job_id": request.job_id,
            "file_path": FIXTURE_BUG_FILE,
            "patched_sha256": _digest_text(patched_body),
            "expected_after_sha256": _digest_text(FIXTURE_AFTER_TEXT),
            "old_text_absent": FIXTURE_BEFORE_TEXT not in patched_body,
            "new_text_present": FIXTURE_AFTER_TEXT in patched_body,
            "original_fixture_immutable": source_immutable,
            "changed_paths": sorted(changed_paths),
            "exact_workspace_scope": exact_scope,
            "processes": {
                "test": test_result,
                "git_diff_check": readback_process,
                "git_status": status_process,
            },
            "verified": readback_ok,
        }
        await _record_artifact(
            request.job_id,
            job_workspace,
            filename="readback.json",
            artifact_type="native_swe_readback",
            payload=readback_payload,
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        await durable_job_repository.record_readback(
            request.job_id,
            target_path=relative_bug_path,
            status="succeeded" if readback_ok else "failed",
            effect_type="native_swe_output",
            target_digest=_digest_text(patched_body),
            content_sha256=_digest_text(patched_body),
            details={
                "readback_artifact": _relative_workspace_path(job_workspace.artifact_dir / "readback.json"),
                "test_exit_code": test_result["exit_code"],
                "independent_process_exit_codes": {
                    "test": test_result["exit_code"],
                    "git_diff_check": readback_process["exit_code"],
                    "git_status": status_process["exit_code"],
                },
                "original_fixture_immutable": source_immutable,
                "verified": readback_ok,
            },
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        await _record_checkpoint(
            request.job_id,
            "readback",
            artifact_path=_relative_workspace_path(job_workspace.artifact_dir / "readback.json"),
            owner=worker_owner,
            fencing_token=fencing_token,
        )
        if not readback_ok:
            durable_job = await _fail_claimed_job(
                request.job_id,
                owner=worker_owner,
                fencing_token=fencing_token,
                reason_code="readback_failed",
            )
            return {
                "status": "failed",
                "reason_code": "readback_failed",
                "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
                "provider": None,
                "workspace": _workspace_receipt(job_workspace),
                "durable_job": durable_job,
                "original_fixture_immutable": source_immutable,
                "operator_visible": True,
            }
        durable_job = await durable_job_repository.transition_job(
            request.job_id,
            "succeeded",
            owner=worker_owner,
            fencing_token=fencing_token,
            result={"readback_sha256": _digest_text(patched_body)},
            result_summary="deterministic fixture patched, tested, and read back",
        )
        return {
            "status": "succeeded",
            "reason_code": "fixture_verified",
            "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
            "provider": None,
            "workspace": _workspace_receipt(job_workspace),
            "durable_job": durable_job,
            "original_fixture_immutable": source_immutable,
            "operator_visible": True,
        }
    except asyncio.CancelledError:
        if execution_control is not None:
            _mark_native_cleanup_requested(request.job_id, execution_control)
        if fencing_token is not None:
            durable_job = await _cancel_claimed_job(
                request.job_id,
                owner=worker_owner,
                fencing_token=fencing_token,
                reason="operator_cancelled",
            )
        raise
    except NativeSoftwareEngineeringError as exc:
        if execution_control is not None and execution_control.cancel_event.is_set() and job_workspace is not None:
            current_job = await durable_job_repository.get_job(request.job_id)
            return _cancellation_result(
                request,
                prepared,
                job_workspace,
                current_job,
                reason_code="operator_cancelled",
            )
        if fencing_token is not None:
            durable_job = await _fail_claimed_job(
                request.job_id,
                owner=worker_owner,
                fencing_token=fencing_token,
                reason_code=exc.reason_code,
                blocked=exc.reason_code.endswith("blocked"),
            )
        return {
            "status": "blocked" if exc.reason_code.endswith("blocked") else "failed",
            "reason_code": exc.reason_code,
            "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
            "provider": None,
            "workspace": _workspace_receipt(job_workspace) if job_workspace else None,
            "durable_job": durable_job,
            "original_fixture_immutable": _fixture_tree_digest(prepared.source) == prepared.source_digest,
            "operator_visible": True,
        }
    except Exception:
        if execution_control is not None and execution_control.cancel_event.is_set() and job_workspace is not None:
            current_job = await durable_job_repository.get_job(request.job_id)
            return _cancellation_result(
                request,
                prepared,
                job_workspace,
                current_job,
                reason_code="operator_cancelled",
            )
        if fencing_token is not None:
            durable_job = await _fail_claimed_job(
                request.job_id,
                owner=worker_owner,
                fencing_token=fencing_token,
                reason_code="native_fixture_execution_error",
            )
        return {
            "status": "failed",
            "reason_code": "native_fixture_execution_error",
            "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
            "provider": None,
            "workspace": _workspace_receipt(job_workspace) if job_workspace else None,
            "durable_job": durable_job,
            "original_fixture_immutable": _fixture_tree_digest(prepared.source) == prepared.source_digest,
            "operator_visible": True,
        }
    finally:
        if cancel_context_token is not None:
            _NATIVE_CANCEL_EVENT.reset(cancel_context_token)
        if job_context_token is not None:
            _NATIVE_JOB_ID.reset(job_context_token)
        if fencing_context_token is not None:
            _NATIVE_FENCING_TOKEN.reset(fencing_context_token)
        if runtime_fencing_token is not None:
            reset_runtime_fencing_token(runtime_fencing_token)
        if runtime_principal_token is not None:
            reset_runtime_trust_principal(runtime_principal_token)
        _unregister_native_execution(request.job_id, execution_control)


async def resume_native_software_engineering_fixture(
    request: NativeSoftwareEngineeringRequest,
    approval_receipt: NativeSoftwareEngineeringApprovalReceipt | None = None,
    *,
    approval_id: str | None = None,
) -> dict[str, Any]:
    """Resume through the repository approval id, never a caller receipt.

    ``run_native_software_engineering_fixture`` performs the atomic
    approved-row consumption and host binding immediately before apply. The
    legacy positional receipt is rejected explicitly so it cannot become an
    alternate authority path.
    """
    if approval_receipt is not None:
        return {
            "status": "blocked",
            "reason_code": "approval_receipt_unsupported",
            "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
            "provider": None,
            "operator_visible": True,
        }
    candidate = replace(
        request,
        patch_approval="approved",
        approval_receipt=None,
        approval_id=approval_id or request.approval_id,
    )
    try:
        _validate_request_identity(candidate)
        _validate_patch_approval(candidate)
    except NativeSoftwareEngineeringError as exc:
        return {
            "status": "blocked",
            "reason_code": exc.reason_code,
            "evidence_mode": NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE,
            "provider": None,
            "operator_visible": True,
        }
    return await run_native_software_engineering_fixture(candidate)


def _native_effect_is_unresolved(effect: Any) -> bool:
    """Mirror the durable unresolved-effect shape for cancellation gating."""
    if not isinstance(effect, dict):
        return False
    if effect.get("reconciled") is True or effect.get("reconciliation_status") in {
        "reconciled",
        "resolved",
    }:
        return False
    if str(effect.get("status") or "") in {"unknown", "intent", "dispatched"}:
        return True
    details = effect.get("details")
    if isinstance(details, dict):
        if details.get("reconciliation_required") or details.get("unknown_cost_outstanding"):
            return True
        nested = details.get("receipt")
        if isinstance(nested, dict) and (
            nested.get("reconciliation_required") or nested.get("unknown_cost_outstanding")
        ):
            return True
    return False


def _native_local_patch_effect(effect: Any, *, job_id: str) -> bool:
    """Recognize only this job's workspace patch as locally reconcilable."""
    if not isinstance(effect, dict) or str(effect.get("effect_type") or "") != "workspace_patch":
        return False
    token = _job_token(job_id)
    prefix = f".seraph/native-software-engineering/jobs/{token}/workspace/"
    target_path = str(effect.get("target_path") or "")
    if not target_path.startswith(prefix) or target_path == prefix:
        return False
    details = effect.get("details")
    return not isinstance(details, dict) or details.get("effect_boundary") in {
        None,
        "apply_workspace_patch",
    }


def _native_only_local_patch_pending(job: dict[str, Any] | None) -> bool:
    """Return true only while the native job's local patch readback is pending.

    Cancellation must continue to preserve ``unknown_external_effect`` for a
    job with any other unresolved receipt.  The native patch is the one
    bounded, in-workspace effect whose runner can finish an exact readback
    without contacting an external system.
    """
    if not isinstance(job, dict) or job.get("job_kind") != NATIVE_SOFTWARE_ENGINEERING_JOB_KIND:
        return False
    effects = job.get("effects")
    if not isinstance(effects, list):
        return False
    unresolved = [item for item in effects if _native_effect_is_unresolved(item)]
    return bool(unresolved) and all(
        _native_local_patch_effect(item, job_id=str(job.get("job_id") or ""))
        for item in unresolved
    )


async def _wait_for_native_local_patch_readback(
    job_id: str,
    current: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Let the in-flight local patch settle before fencing cancellation.

    ``patch.json`` is intentionally written before its durable readback, so a
    caller can observe the artifact while that readback CAS is still queued.
    A short bounded wait closes that harmless local race.  If the readback
    does not settle, the normal durable transition remains fail-closed.
    """
    if not _native_only_local_patch_pending(current):
        return current
    deadline = asyncio.get_running_loop().time() + _NATIVE_CANCEL_SETTLE_SECONDS
    while _native_only_local_patch_pending(current):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(_NATIVE_CANCEL_POLL_SECONDS, remaining))
        current = await durable_job_repository.get_job(job_id)
        if current is None:
            break
    return current


async def cancel_native_software_engineering_job(
    job_id: str,
    *,
    owner: str,
    fencing_token: int,
    reason: str = "operator_cancelled",
) -> dict[str, Any]:
    """Cancel a claimed fixture job and signal its bounded native process.

    Durable owner/fence validation remains authoritative.  The in-process
    event is set only after that transition succeeds, so an unauthorized
    caller cannot kill another job's test process; a runner that observes the
    event cannot produce a success receipt.
    """
    last_lease_error: DurableJobLeaseError | None = None
    result: dict[str, Any] | None = None
    for _attempt in range(_NATIVE_CANCEL_CAS_ATTEMPTS):
        current = await durable_job_repository.get_job(job_id)
        current = await _wait_for_native_local_patch_readback(job_id, current)
        expected_revision = current.get("revision") if isinstance(current, dict) else None
        try:
            result = await durable_job_repository.cancel_job(
                job_id,
                owner=owner,
                fencing_token=fencing_token,
                expected_revision=(int(expected_revision) if expected_revision is not None else None),
                reason=reason,
            )
            break
        except DurableJobLeaseError as exc:
            last_lease_error = exc
            latest = await durable_job_repository.get_job(job_id)
            if not isinstance(latest, dict):
                raise
            latest_status = str(latest.get("status") or "")
            if latest_status in {"cancelled", "unknown_external_effect", "cost_liability", "blocked"}:
                result = latest
                break
            lease = latest.get("lease")
            if (
                latest_status != "running"
                or not isinstance(lease, dict)
                or lease.get("owner") != owner
                or int(lease.get("fencing_token") or 0) != int(fencing_token)
            ):
                raise
            # A runner checkpoint may have advanced only the revision. Retry
            # against the freshly read fenced row; owner and fence never
            # change as part of this retry.
            await asyncio.sleep(0)
    if result is None:
        if last_lease_error is not None:
            raise last_lease_error
        raise DurableJobLeaseError("native cancellation did not produce a durable result")
    if result.get("status") in {"cancelled", "unknown_external_effect", "cost_liability", "blocked"}:
        control = _native_execution_for_job(job_id)
        if control is not None and control.owner == owner and control.fencing_token == fencing_token:
            _mark_native_cleanup_requested(job_id, control)
    return result


__all__ = [
    "FIXTURE_AFTER_TEXT",
    "FIXTURE_BEFORE_TEXT",
    "FIXTURE_BUG_FILE",
    "FIXTURE_TEST_ARGS",
    "FIXTURE_TEST_COMMAND",
    "FIXTURE_TEST_FILE",
    "NATIVE_SOFTWARE_ENGINEERING_CAPABILITY_ID",
    "NATIVE_SOFTWARE_ENGINEERING_CAPABILITY_VERSION",
    "NATIVE_SOFTWARE_ENGINEERING_EVIDENCE_MODE",
    "NativeSoftwareEngineeringError",
    "NativeSoftwareEngineeringApprovalReceipt",
    "NativeSoftwareEngineeringRequest",
    "build_native_software_engineering_approval_receipt",
    "build_native_software_engineering_plan",
    "cancel_native_software_engineering_job",
    "native_software_engineering_fixture_root",
    "preflight_native_software_engineering_fixture",
    "resume_native_software_engineering_fixture",
    "run_native_software_engineering_fixture",
]
