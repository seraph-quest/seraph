"""Durable, public read-only browser task runner.

This module intentionally exposes a small capability grammar.  It is not a
computer-use agent: the only actions are public HTTPS navigation and bounded
DOM extraction.  Work Board and input-artifact authority is supplied by the
server, while this runner owns the browser durable root and its lease/effect
receipts through :mod:`src.workflows.job_runtime`.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import sys
import stat
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from config.settings import settings
from src.security.site_policy import evaluate_site_access
from src.workspace import canonical_workspace_root
from src.workflows.job_runtime import (
    DurableJobAdmissionDenied,
    DurableJobError,
    DurableJobIdentity,
    DurableJobRepository,
    DurableJobSpec,
    durable_job_repository,
)

from .pinned_transport import (
    BROWSER_ALLOWED_ATTRIBUTES,
    PinnedBrowserTransport,
    PinnedTransportError,
    _evaluate_site_policy,
    _resolve_all,
    normalize_allowed_hosts,
    normalize_approved_prefixes,
    parse_public_https_url,
    safe_url,
    url_digest,
)


BROWSER_TASK_CAPABILITY_ID = "browser.public-task.v1"
BROWSER_TASK_CAPABILITY_VERSION = "1"
BROWSER_TASK_JOB_KIND = "browser_public_task"
BROWSER_TASK_SERVICE_ID = "service:browser-task"
BROWSER_TASK_OWNER_PRINCIPAL = BROWSER_TASK_SERVICE_ID
BROWSER_MAX_ACTIONS = 8
BROWSER_MAX_CHECKS = 8
BROWSER_MAX_HOSTS = 8
BROWSER_MAX_PREFIXES = 8
BROWSER_MAX_NAVIGATIONS = 8
BROWSER_MAX_REQUESTS = 32
BROWSER_MAX_RUNTIME_SECONDS = 180
BROWSER_MAX_ATTEMPTS = 2
BROWSER_MAX_EXTRACT_CHARS = 65_536
BROWSER_MAX_EXTRACT_BYTES = 65_536
BROWSER_MAX_FIELD_BYTES = 2 * 1024
BROWSER_CLEANUP_TIMEOUT_SECONDS = 10
BROWSER_CLEANUP_RESERVE_FRACTION = 0.20
BROWSER_ARTIFACT_DIR = "artifacts/work-board/browser"
_PRELAUNCH_ADMISSION_REASONS = frozenset({"goal_budget_outstanding_limit"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_BROWSER_ARTIFACT_PATH = re.compile(
    r"^artifacts/work-board/browser/result-[0-9a-f]{32}\.json$"
)


def browser_artifact_path_for_job(job_id: str) -> str:
    """Return the only result path the browser writer may publish for a job."""

    return f"{BROWSER_ARTIFACT_DIR}/result-{_digest(str(job_id))[:32]}.json"


def _open_browser_artifact_descriptor(
    relative_path: str,
    *,
    workspace_root: str | Path | None = None,
) -> int | None:
    """Open a canonical browser result through held no-follow descriptors.

    The caller must derive ``relative_path`` from the durable job identity.  A
    lexical containment check is insufficient here because a parent directory
    or final result can be swapped for a symlink between validation and read.
    Returning a descriptor keeps the verified inode stable for the bounded
    read and digest calculation.
    """

    candidate = PurePosixPath(str(relative_path))
    if not _BROWSER_ARTIFACT_PATH.fullmatch(candidate.as_posix()):
        return None
    try:
        root = canonical_workspace_root(workspace_root or settings.workspace_dir)
    except (OSError, TypeError, ValueError):
        return None
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec
    parent_fd: int | None = None
    try:
        parent_fd = os.open(root, directory_flags)
        for component in candidate.parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        descriptor = os.open(
            candidate.parts[-1],
            os.O_RDONLY | nofollow | cloexec,
            dir_fd=parent_fd,
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            os.close(descriptor)
            return None
        return descriptor
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _open_browser_artifact_parent(
    relative_path: str,
    *,
    workspace_root: str | Path | None = None,
    create_parents: bool = False,
) -> int | None:
    """Open the canonical browser-result directory without following links.

    The writer keeps this descriptor open while creating the temporary file
    and replacing the final name.  This makes the operation relative to the
    verified directory inode instead of a mutable ``Path`` string.
    """

    candidate = PurePosixPath(str(relative_path))
    if not _BROWSER_ARTIFACT_PATH.fullmatch(candidate.as_posix()):
        return None
    try:
        root = canonical_workspace_root(workspace_root or settings.workspace_dir)
    except (OSError, TypeError, ValueError):
        return None
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec
    parent_fd: int | None = None
    try:
        parent_fd = os.open(root, directory_flags)
        for component in candidate.parts[:-1]:
            if create_parents:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        if parent_fd is None:
            return None
        parent_stat = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_stat.st_mode):
            return None
        descriptor = parent_fd
        parent_fd = None
        return descriptor
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _write_browser_artifact_bytes(
    relative_path: str,
    payload: bytes,
    *,
    workspace_root: str | Path | None = None,
) -> None:
    """Atomically publish one bounded browser result through directory fds.

    The caller has already validated the serialized payload.  This helper
    owns only the fixed browser-result path and never follows an attacker-
    controlled parent, temporary, or final symlink.
    """

    if not isinstance(payload, bytes) or len(payload) > BROWSER_MAX_EXTRACT_BYTES:
        raise OSError("browser artifact payload is invalid")
    parent_fd = _open_browser_artifact_parent(
        relative_path,
        workspace_root=workspace_root,
        create_parents=True,
    )
    if parent_fd is None:
        raise OSError("browser artifact directory is unavailable")
    final_name = PurePosixPath(relative_path).name
    temporary_name = f".{final_name}.{hashlib.sha256(payload).hexdigest()[:12]}.tmp"
    temp_fd: int | None = None
    published = False
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        temp_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec,
            0o600,
            dir_fd=parent_fd,
        )
        view = memoryview(payload)
        while view:
            written = os.write(temp_fd, view)
            if written <= 0:
                raise OSError("browser artifact write made no progress")
            view = view[written:]
        os.fchmod(temp_fd, 0o600)
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        os.replace(
            temporary_name,
            final_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        published = True
        os.fsync(parent_fd)
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass


def read_browser_artifact_bytes(
    relative_path: str,
    *,
    workspace_root: str | Path | None = None,
    max_bytes: int = BROWSER_MAX_EXTRACT_BYTES,
) -> bytes | None:
    """Read one canonical browser result with a hard byte bound.

    This helper is intentionally read-only and returns no diagnostic detail;
    callers must expose a single bounded unavailable state for missing,
    swapped, malformed, or oversized workspace files.
    """

    if type(max_bytes) is not int or max_bytes < 1:
        return None
    descriptor = _open_browser_artifact_descriptor(
        relative_path,
        workspace_root=workspace_root,
    )
    if descriptor is None:
        return None
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                return None
            payload = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                return None
        return payload if len(payload) <= max_bytes else None
    except (OSError, ValueError):
        try:
            os.close(descriptor)
        except OSError:
            pass
        return None


class BrowserTaskError(RuntimeError):
    """Typed runner failure with an operator-safe reason code."""

    def __init__(self, message: str, *, code: str, dispatched: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.dispatched = dispatched
        self.cleanup_status = "cleanup_unknown"
        self.observed_request_receipts: list[dict[str, Any]] = []


class BrowserInputError(BrowserTaskError):
    def __init__(self, message: str, *, code: str = "input_invalid") -> None:
        super().__init__(message, code=code, dispatched=False)


class BrowserVerificationError(BrowserTaskError):
    def __init__(self, message: str, *, code: str = "verification_failed") -> None:
        super().__init__(message, code=code, dispatched=True)


class BrowserUnknownExternalEffect(BrowserTaskError):
    def __init__(self, message: str, *, code: str = "unknown_external_effect") -> None:
        super().__init__(message, code=code, dispatched=True)


class BrowserRuntimeControls(Protocol):
    """Narrow server callback used to revalidate the Work Board fence.

    The dispatcher owns the implementation.  It is deliberately a guard,
    not a second execution ledger: it raises on stale task/attempt/session/
    goal/artifact authority and returns no client-controlled identity.
    """

    async def assert_current(self, **kwargs: Any) -> Any: ...


def _canonical_json(value: Any) -> str:
    # Match the canonical durable-job digest format used by the dispatcher and
    # ``job_runtime`` so admission/link identity remains stable for Unicode
    # selectors and expected-check text as well as ASCII inputs.
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _browser_input_digests(model: "BrowserTaskInput") -> tuple[str, str, str]:
    """Return the immutable artifact, model, and consent digests."""

    model_json = model.model_dump(mode="json", exclude_none=True)
    envelope_digest = _digest(
        {
            "schema_version": 1,
            "capability_id": BROWSER_TASK_CAPABILITY_ID,
            "input": model_json,
        }
    )
    model_digest = _digest(model_json)
    consent_digest = _digest(
        {
            "allowed_hosts": model.allowed_hosts,
            "approved_url_prefixes": model.approved_url_prefixes,
            "actions": [action.model_dump(mode="json", exclude_none=True) for action in model.actions],
        }
    )
    return envelope_digest, model_digest, consent_digest


def _text(value: Any, default: str = "") -> str:
    return str(value or "").strip() or default


def _safe_identifier(value: str, *, field_name: str) -> str:
    normalized = _text(value)
    if not _SAFE_ID.fullmatch(normalized):
        raise BrowserInputError(f"{field_name} is not a bounded opaque identifier", code=f"{field_name}_invalid")
    return normalized


def _safe_digest(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = _text(value).lower()
    if not _SHA256.fullmatch(normalized):
        raise BrowserInputError(f"{field_name} must be a SHA-256 digest", code=f"{field_name}_invalid")
    return normalized


def _safe_url_value(value: str, *, field_name: str) -> str:
    if len(value.encode("utf-8")) > BROWSER_MAX_FIELD_BYTES:
        raise ValueError(f"{field_name} exceeds the 2 KiB limit")
    parse_public_https_url(value)
    return value.strip()


def _path_prefix_match(path: str, prefix: str) -> bool:
    path = path or "/"
    prefix = prefix or "/"
    if path == prefix:
        return True
    boundary = prefix.rstrip("/")
    return not boundary or path.startswith(boundary + "/")


class BrowserExpectedCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["url_host", "url_path_prefix", "text_contains", "text_sha256"]
    selector: str | None = None
    value: str = Field(min_length=1, max_length=BROWSER_MAX_FIELD_BYTES)

    @field_validator("selector")
    @classmethod
    def selector_bound(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > BROWSER_MAX_FIELD_BYTES:
            raise ValueError("selector exceeds the 2 KiB limit")
        return value

    @field_validator("value")
    @classmethod
    def value_bound(cls, value: str) -> str:
        if len(value.encode("utf-8")) > BROWSER_MAX_FIELD_BYTES:
            raise ValueError("check value exceeds the 2 KiB limit")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> "BrowserExpectedCheck":
        if self.kind in {"text_contains", "text_sha256"} and not self.selector:
            raise ValueError("text checks require a selector")
        if self.kind in {"url_host", "url_path_prefix"} and self.selector is not None:
            raise ValueError("URL checks do not accept a selector")
        if self.kind == "text_sha256" and not _SHA256.fullmatch(self.value):
            raise ValueError("text_sha256 checks require lowercase SHA-256")
        if self.kind == "url_host":
            normalize_allowed_hosts([self.value])
        if self.kind == "url_path_prefix" and not self.value.startswith("/"):
            raise ValueError("url_path_prefix checks require an absolute path")
        return self


class BrowserAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["navigate", "extract"]
    url: str | None = None
    selector: str | None = None
    max_chars: int | None = None
    attribute: str | None = None
    expected_checks: list[BrowserExpectedCheck] = Field(min_length=1, max_length=BROWSER_MAX_CHECKS)

    @field_validator("url")
    @classmethod
    def url_bound(cls, value: str | None) -> str | None:
        return _safe_url_value(value, field_name="action.url") if value is not None else None

    @field_validator("selector")
    @classmethod
    def selector_bound(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > BROWSER_MAX_FIELD_BYTES:
            raise ValueError("selector exceeds the 2 KiB limit")
        return value

    @field_validator("max_chars")
    @classmethod
    def max_chars_bound(cls, value: int | None) -> int | None:
        if value is not None and (type(value) is not int or not 1 <= value <= BROWSER_MAX_EXTRACT_CHARS):
            raise ValueError("max_chars must be between 1 and 65536")
        return value

    @field_validator("attribute")
    @classmethod
    def attribute_bound(cls, value: str | None) -> str | None:
        if value is not None and value not in BROWSER_ALLOWED_ATTRIBUTES:
            raise ValueError("attribute is not in the fixed read-only allowlist")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> "BrowserAction":
        if self.kind == "navigate":
            if not self.url:
                raise ValueError("navigate actions require url")
            if self.selector is not None or self.max_chars is not None or self.attribute is not None:
                raise ValueError("navigate actions accept only url and expected_checks")
        else:
            if not self.selector:
                raise ValueError("extract actions require selector")
            if self.url is not None:
                raise ValueError("extract actions do not accept url")
            if self.max_chars is None:
                raise ValueError("extract actions require max_chars")
        return self


class BrowserTaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    start_url: str = Field(min_length=1, max_length=BROWSER_MAX_FIELD_BYTES)
    allowed_hosts: list[str] = Field(min_length=1, max_length=BROWSER_MAX_HOSTS)
    approved_url_prefixes: list[str] = Field(min_length=1, max_length=BROWSER_MAX_PREFIXES)
    actions: list[BrowserAction] = Field(min_length=1, max_length=BROWSER_MAX_ACTIONS)
    final_expected_checks: list[BrowserExpectedCheck] = Field(min_length=1, max_length=BROWSER_MAX_CHECKS)

    @field_validator("start_url")
    @classmethod
    def start_url_bound(cls, value: str) -> str:
        return _safe_url_value(value, field_name="start_url")

    @field_validator("allowed_hosts")
    @classmethod
    def hosts_bound(cls, value: list[str]) -> list[str]:
        if len(value) > BROWSER_MAX_HOSTS:
            raise ValueError("at most eight allowed hosts are supported")
        return list(normalize_allowed_hosts(value))

    @field_validator("approved_url_prefixes")
    @classmethod
    def prefixes_bound(cls, value: list[str]) -> list[str]:
        return list(normalize_approved_prefixes(value))

    @model_validator(mode="after")
    def validate_consented_urls(self) -> "BrowserTaskInput":
        prefixes = tuple(self.approved_url_prefixes)
        allowed_hosts = set(normalize_allowed_hosts(self.allowed_hosts))
        if any(
            normalize_allowed_hosts([parse_public_https_url(prefix).hostname or ""])[0] not in allowed_hosts
            for prefix in prefixes
        ):
            raise ValueError("approved URL prefixes must use explicitly allowed hosts")
        if not any(_prefix_matches(self.start_url, prefix) for prefix in prefixes):
            raise ValueError("start_url is outside approved URL prefixes")
        for action in self.actions:
            if action.kind == "navigate" and not any(_prefix_matches(action.url or "", prefix) for prefix in prefixes):
                raise ValueError("navigate URL is outside approved URL prefixes")
        return self


class BrowserTaskReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    capability_id: Literal["browser.public-task.v1"]
    task_id: str
    attempt_id: str
    job_id: str
    status: Literal[
        "admitted",
        "succeeded",
        "blocked",
        "cancelled",
        "unknown_external_effect",
        "degraded",
    ]
    durable_status: str
    reason_code: str | None = None
    action_count: int = Field(ge=0, le=BROWSER_MAX_ACTIONS)
    request_count: int = Field(ge=0, le=BROWSER_MAX_REQUESTS)
    actual_page_url: str | None = None
    actual_page_url_digest: str | None = None
    action_receipts: list[dict[str, Any]] = Field(default_factory=list, max_length=BROWSER_MAX_ACTIONS)
    request_receipts: list[dict[str, Any]] = Field(default_factory=list, max_length=BROWSER_MAX_REQUESTS)
    artifact_ref: str | None = None
    artifact_sha256: str | None = None
    readback_id: str | None = None
    verified_at: str | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list, max_length=BROWSER_MAX_CHECKS * (BROWSER_MAX_ACTIONS + 1))
    cleanup_status: Literal["not_needed", "cleanup_verified", "cleanup_unknown"] = "not_needed"
    memory_status: Literal["no_learning"] = "no_learning"


class BrowserPreflightReceipt(BaseModel):
    """Provider-free dependency/policy result used before a Work Board claim."""

    model_config = ConfigDict(extra="forbid", strict=True)

    capability_id: Literal["browser.public-task.v1"]
    status: Literal["ready", "blocked"]
    reason_code: str | None = None
    checked_hosts: list[str] = Field(default_factory=list, max_length=BROWSER_MAX_HOSTS)
    playwright_installed: bool
    browser_executable_present: bool


@dataclass(slots=True)
class _BrowserSession:
    browser: Any
    context: Any
    playwright: Any = None

    async def close(self) -> bool:
        clean = True
        try:
            await self.context.close()
        except BaseException:
            clean = False
        try:
            await self.browser.close()
        except BaseException:
            clean = False
        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except BaseException:
                clean = False
        return clean


@dataclass(slots=True)
class _BrowserLaunchResources:
    """Resources retained across a partially completed browser launch.

    Playwright can create a driver, browser, or context before the await that
    returns the final ``_BrowserSession`` completes.  Keeping each handle here
    makes cancellation and timeout cleanup fail closed instead of turning an
    interrupted launch into a false ``not_needed`` receipt.
    """

    launch_attempted: bool = False
    playwright: Any = None
    browser: Any = None
    context: Any = None
    session: _BrowserSession | None = None
    cleanup_task: asyncio.Task[Any] | None = None
    cleanup_result: bool | None = None

    @property
    def has_resources(self) -> bool:
        return any(value is not None for value in (self.playwright, self.browser, self.context, self.session))

    @property
    def context_not_started(self) -> bool:
        # A launch attempt with an unknown result is not a known no-context
        # outcome, even when no handle was returned to this coroutine.
        return not self.launch_attempted and not self.has_resources

    async def close(self) -> bool:
        if self.session is not None:
            return await self.session.close()

        clean = True
        for resource, method_name in (
            (self.context, "close"),
            (self.browser, "close"),
            (self.playwright, "stop"),
        ):
            if resource is None:
                continue
            try:
                result = getattr(resource, method_name)()
                if inspect.isawaitable(result):
                    await result
            except BaseException:
                clean = False
        return clean


def _safe_receipt_url(url: str | None) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    try:
        return safe_url(url), url_digest(url)
    except Exception:
        return None, None


def _playwright_browser_executable_present() -> bool:
    """Check Playwright's local driver/browser files without launching them."""

    try:
        import playwright
        from playwright._impl._driver import compute_driver_executable

        driver, cli = compute_driver_executable()
        if not Path(driver).is_file() or not Path(cli).is_file():
            return False
        package_root = Path(playwright.__file__).resolve().parent
    except (ImportError, OSError, TypeError, ValueError):
        return False

    configured_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if configured_root == "0":
        roots = [package_root / ".local-browsers"]
    elif configured_root:
        roots = [Path(configured_root).expanduser()]
    else:
        roots = [Path.home() / ".cache" / "ms-playwright"]
    executable_names = {
        "chrome",
        "chrome.exe",
        "chrome-headless-shell",
        "chrome-headless-shell.exe",
        "chromium",
        "chromium.exe",
    }
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for current, directories, files in os.walk(root):
                relative_depth = len(Path(current).relative_to(root).parts)
                if relative_depth >= 5:
                    directories[:] = []
                if any(
                    name in executable_names
                    and os.access(Path(current) / name, os.X_OK)
                    for name in files
                ):
                    return True
        except (OSError, ValueError):
            continue
    return False


class BrowserTaskRunner:
    """Execute one server-admitted public browser task."""

    def __init__(
        self,
        *,
        jobs: DurableJobRepository | Any | None = None,
        transport_factory: Callable[[], PinnedBrowserTransport] | None = None,
        browser_launcher: Callable[[], Any] | None = None,
        runtime_controls: BrowserRuntimeControls | Callable[..., Any] | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        self.jobs = jobs or durable_job_repository
        self.transport_factory = transport_factory or (lambda: PinnedBrowserTransport())
        self.browser_launcher = browser_launcher
        self.runtime_controls = runtime_controls
        self.workspace_root = (
            canonical_workspace_root(workspace_root)
            if workspace_root is not None
            else canonical_workspace_root(settings.workspace_dir)
        )

    async def preflight(
        self,
        inputs: BrowserTaskInput | Mapping[str, Any],
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        """Validate policy, DNS and local browser dependencies before claiming a task.

        This method performs no browser launch, HTTP request, durable mutation,
        or account/provider contact.  The dispatcher may use the typed result
        before it claims a Work Board attempt; execution repeats the transport
        checks on every fresh request.
        """

        checked_hosts: list[str] = []
        playwright_installed = False
        browser_executable_present = False
        transport: PinnedBrowserTransport | None = None
        try:
            model = inputs if isinstance(inputs, BrowserTaskInput) else BrowserTaskInput.model_validate(inputs)
            transport = self.transport_factory()
            try:
                import playwright  # noqa: F401
            except ImportError:
                raise BrowserInputError("Playwright is not installed", code="playwright_unavailable")
            playwright_installed = True
            browser_executable_present = _playwright_browser_executable_present()
            if not browser_executable_present:
                transport.cancel_pending_blocking()
                return BrowserPreflightReceipt(
                    capability_id=BROWSER_TASK_CAPABILITY_ID,
                    status="blocked",
                    reason_code="browser_executable_unavailable",
                    checked_hosts=[],
                    playwright_installed=playwright_installed,
                    browser_executable_present=False,
                ).model_dump(mode="json")
            normalized_hosts = normalize_allowed_hosts(model.allowed_hosts)
            bounded_timeout = max(
                0.001,
                min(float(timeout_seconds), float(getattr(transport, "timeout_seconds", 10.0))),
            )
            for hostname in normalized_hosts:
                checked_hosts.append(hostname)
                host_url = f"https://[{hostname}]/" if ":" in hostname else f"https://{hostname}/"
                decision = await _evaluate_site_policy(
                    transport.site_policy,
                    host_url,
                    timeout_seconds=bounded_timeout,
                    pending=transport._pending_blocking,
                )
                if not decision.allowed:
                    transport.cancel_pending_blocking()
                    return BrowserPreflightReceipt(
                        capability_id=BROWSER_TASK_CAPABILITY_ID,
                        status="blocked",
                        reason_code="site_policy_blocked",
                        checked_hosts=checked_hosts,
                        playwright_installed=playwright_installed,
                        browser_executable_present=browser_executable_present,
                    ).model_dump(mode="json")
                await asyncio.wait_for(
                    _resolve_all(
                        transport.resolver,
                        hostname,
                        443,
                        timeout_seconds=bounded_timeout,
                        pending=transport._pending_blocking,
                    ),
                    timeout=bounded_timeout,
                )
            transport.cancel_pending_blocking()
            return BrowserPreflightReceipt(
                capability_id=BROWSER_TASK_CAPABILITY_ID,
                status="ready",
                reason_code=None,
                checked_hosts=checked_hosts,
                playwright_installed=playwright_installed,
                browser_executable_present=browser_executable_present,
            ).model_dump(mode="json")
        except ValidationError:
            reason_code = "input_invalid"
        except BrowserTaskError as exc:
            reason_code = exc.code
        except PinnedTransportError as exc:
            reason_code = exc.code
        except asyncio.TimeoutError:
            reason_code = "preflight_timeout"
        except (ImportError, OSError, TypeError, ValueError):
            reason_code = "browser_dependency_unavailable"
        if transport is not None:
            transport.cancel_pending_blocking()
        return BrowserPreflightReceipt(
            capability_id=BROWSER_TASK_CAPABILITY_ID,
            status="blocked",
            reason_code=reason_code,
            checked_hosts=checked_hosts,
            playwright_installed=playwright_installed,
            browser_executable_present=browser_executable_present,
        ).model_dump(mode="json")

    async def run(
        self,
        *,
        task_id: str,
        attempt_id: str,
        owner_principal_id: str,
        owner_session_id: str,
        goal_id: str | None,
        goal_revision: int | None,
        board_task_revision: int,
        board_fencing_token: int,
        input_artifact_id: str,
        inputs: BrowserTaskInput | Mapping[str, Any],
        runtime_seconds: int,
        admission_only: bool,
        task_priority: int,
        admission_board_task_revision: int | None = None,
        durable_job_id: str | None = None,
        durable_lease_owner: str | None = None,
        durable_fencing_token: int | None = None,
        input_artifact_digest: str | None = None,
        effective_max_attempts: int | None = None,
        effective_max_outstanding_jobs: int | None = None,
        routine_parent_job_id: str | None = None,
        routine_parent_fencing_token: int | None = None,
        routine_step_id: str | None = None,
    ) -> dict[str, Any]:
        task_id = _safe_identifier(task_id, field_name="task_id")
        attempt_id = _safe_identifier(attempt_id, field_name="attempt_id")
        owner_principal_id = _safe_identifier(owner_principal_id, field_name="owner_principal_id")
        owner_session_id = _safe_identifier(owner_session_id, field_name="owner_session_id")
        input_artifact_id = _safe_identifier(input_artifact_id, field_name="input_artifact_id")
        try:
            goal_id = _safe_identifier(goal_id or "", field_name="goal_id")
        except BrowserInputError as exc:
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                status="blocked",
                durable_status="blocked",
                reason_code=exc.code,
            )
        if type(goal_revision) is not int or goal_revision < 1:
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                status="blocked",
                durable_status="blocked",
                reason_code="goal_revision_invalid",
            )
        try:
            input_artifact_digest = _safe_digest(input_artifact_digest, field_name="input_artifact_digest")
        except BrowserInputError as exc:
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                status="blocked",
                durable_status="blocked",
                reason_code=exc.code,
            )
        if input_artifact_digest is None:
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                status="blocked",
                durable_status="blocked",
                reason_code="input_artifact_digest_invalid",
            )
        if type(board_task_revision) is not int or board_task_revision < 1:
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                status="blocked",
                durable_status="blocked",
                reason_code="board_revision_invalid",
            )
        if admission_board_task_revision is None:
            admission_board_task_revision = board_task_revision
        if type(admission_board_task_revision) is not int or admission_board_task_revision < 1:
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                status="blocked",
                durable_status="blocked",
                reason_code="admission_board_revision_invalid",
            )
        if type(board_fencing_token) is not int or board_fencing_token < 1:
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                status="blocked",
                durable_status="blocked",
                reason_code="board_fence_invalid",
            )
        job_id = durable_job_id or f"browser-task:{task_id}:{attempt_id}"
        if not _SAFE_ID.fullmatch(job_id):
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                job_id=job_id,
                status="blocked",
                durable_status="blocked",
                reason_code="job_id_invalid",
            )
        try:
            task_priority = self._task_priority(task_priority)
        except BrowserInputError as exc:
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                job_id=job_id,
                status="blocked",
                durable_status="blocked",
                reason_code=exc.code,
            )
        runtime_seconds = self._runtime_seconds(runtime_seconds)
        try:
            max_attempts = self._effective_max_attempts(effective_max_attempts)
            max_outstanding_jobs = self._effective_max_outstanding_jobs(effective_max_outstanding_jobs)
        except BrowserInputError as exc:
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                job_id=job_id,
                status="blocked",
                durable_status="blocked",
                reason_code=exc.code,
            )
        try:
            model = inputs if isinstance(inputs, BrowserTaskInput) else BrowserTaskInput.model_validate(inputs)
        except (ValidationError, ValueError, PinnedTransportError) as exc:
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                job_id=job_id,
                status="blocked",
                durable_status="blocked",
                reason_code="input_invalid",
            )
        envelope_digest, model_digest, consent_digest = _browser_input_digests(model)
        if input_artifact_digest != envelope_digest:
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                job_id=job_id,
                status="blocked",
                durable_status="blocked",
                reason_code="input_artifact_digest_mismatch",
            )

        # A production runner must have the dispatcher-owned live authority
        # callback before it can create or resume a durable browser root.  The
        # provider-free ``preflight`` method remains usable without it.
        if self.runtime_controls is None:
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                job_id=job_id,
                status="blocked",
                durable_status="blocked",
                reason_code="runtime_control_required",
            )

        try:
            if admission_only:
                control = self.runtime_controls
                callback = getattr(control, "assert_current", None)
                if callback is None and callable(control):
                    callback = control
                if callback is None:
                    return self._receipt(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        job_id=job_id,
                        status="blocked",
                        durable_status="blocked",
                        reason_code="runtime_control_invalid",
                    )
                try:
                    current = callback(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        board_task_revision=board_task_revision,
                        board_fencing_token=board_fencing_token,
                        owner_principal_id=owner_principal_id,
                        owner_session_id=owner_session_id,
                        goal_id=goal_id,
                        goal_revision=goal_revision,
                        input_artifact_id=input_artifact_id,
                        input_artifact_digest=input_artifact_digest,
                        routine_parent_job_id=routine_parent_job_id,
                        routine_parent_fencing_token=routine_parent_fencing_token,
                    )
                    if inspect.isawaitable(current):
                        current = await current
                except Exception:
                    current = False
                if current is not True:
                    return self._receipt(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        job_id=job_id,
                        status="blocked",
                        durable_status="blocked",
                        reason_code="board_fence_stale",
                    )
                return await self._admit(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    goal_id=goal_id,
                    goal_revision=goal_revision,
                    board_task_revision=board_task_revision,
                    board_fencing_token=board_fencing_token,
                    task_priority=task_priority,
                    input_artifact_id=input_artifact_id,
                    input_artifact_digest=input_artifact_digest,
                    input_envelope_digest=envelope_digest,
                    input_model_digest=model_digest,
                    action_consent_digest=consent_digest,
                    model=model,
                    runtime_seconds=runtime_seconds,
                    job_id=job_id,
                    max_attempts=max_attempts,
                    max_outstanding_jobs=max_outstanding_jobs,
                    routine_parent_job_id=routine_parent_job_id,
                    routine_parent_fencing_token=routine_parent_fencing_token,
                    routine_step_id=routine_step_id,
                )
            return await self._execute(
                task_id=task_id,
                attempt_id=attempt_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                board_task_revision=board_task_revision,
                board_fencing_token=board_fencing_token,
                admission_board_task_revision=admission_board_task_revision,
                task_priority=task_priority,
                input_artifact_id=input_artifact_id,
                input_artifact_digest=input_artifact_digest,
                input_envelope_digest=envelope_digest,
                input_model_digest=model_digest,
                action_consent_digest=consent_digest,
                model=model,
                runtime_seconds=runtime_seconds,
                job_id=job_id,
                durable_lease_owner=durable_lease_owner,
                durable_fencing_token=durable_fencing_token,
                max_attempts=max_attempts,
                max_outstanding_jobs=max_outstanding_jobs,
                routine_parent_job_id=routine_parent_job_id,
                routine_parent_fencing_token=routine_parent_fencing_token,
            )
        except BrowserTaskError as exc:
            return await self._failure_receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                job_id=job_id,
                error=exc,
            )
        except DurableJobAdmissionDenied as exc:
            reason = _text(getattr(exc, "reason", None))
            if reason not in _PRELAUNCH_ADMISSION_REASONS:
                # Only the known quota refusal is proven to happen before a
                # durable row is inserted.  Preserve reconciliation for any
                # future denial raised at a later transaction boundary.
                error = BrowserUnknownExternalEffect(
                    type(exc).__name__,
                    code="runner_unexpected_failure",
                )
                return await self._failure_receipt(
                    task_id=task_id,
                    attempt_id=attempt_id,
                    job_id=job_id,
                    error=error,
                )
            error = BrowserTaskError(
                "browser durable admission was denied before launch",
                code=reason,
                dispatched=False,
            )
            error.cleanup_status = "not_needed"
            return await self._failure_receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                job_id=job_id,
                error=error,
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            error = BrowserUnknownExternalEffect(
                type(exc).__name__,
                code="runner_unexpected_failure",
            )
            return await self._failure_receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                job_id=job_id,
                error=error,
            )

    @staticmethod
    def _runtime_seconds(value: int) -> int:
        if type(value) is not int or value <= 0:
            raise BrowserInputError("runtime_seconds must be a positive integer", code="runtime_invalid")
        return min(value, BROWSER_MAX_RUNTIME_SECONDS)

    @staticmethod
    def _effective_max_attempts(value: int | None) -> int:
        if value is None:
            return BROWSER_MAX_ATTEMPTS
        if type(value) is not int or not 1 <= value <= BROWSER_MAX_ATTEMPTS:
            raise BrowserInputError(
                "effective max attempts exceed the browser capability bound",
                code="effective_attempts_invalid",
            )
        return value

    @staticmethod
    def _effective_max_outstanding_jobs(value: int | None) -> int | None:
        if value is None:
            return None
        if type(value) is not int or not 1 <= value <= 16:
            raise BrowserInputError(
                "effective outstanding-job limit is invalid",
                code="effective_outstanding_invalid",
            )
        return value

    @staticmethod
    def _task_priority(value: int) -> int:
        """Normalize the server-supplied board priority for durable admission."""

        if type(value) is not int or not 0 <= value <= 100:
            raise BrowserInputError(
                "task priority must be between 0 and 100",
                code="priority_invalid",
            )
        return value

    async def _admit(
        self,
        *,
        task_id: str,
        attempt_id: str,
        owner_principal_id: str,
        owner_session_id: str,
        goal_id: str | None,
        goal_revision: int | None,
        board_task_revision: int,
        board_fencing_token: int,
        task_priority: int,
        input_artifact_id: str,
        input_artifact_digest: str | None,
        input_envelope_digest: str,
        input_model_digest: str,
        action_consent_digest: str,
        model: BrowserTaskInput,
        runtime_seconds: int,
        job_id: str,
        max_attempts: int,
        max_outstanding_jobs: int | None,
        routine_parent_job_id: str | None = None,
        routine_parent_fencing_token: int | None = None,
        routine_step_id: str | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        safe_inputs = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "capability_id": BROWSER_TASK_CAPABILITY_ID,
            "input_artifact_id": input_artifact_id,
            "input_artifact_digest": input_artifact_digest,
            "browser_input": model.model_dump(mode="json", exclude_none=True),
            "input_digest": input_model_digest,
            "action_consent_digest": action_consent_digest,
        }
        declared_authority = {
            "principal": BROWSER_TASK_OWNER_PRINCIPAL,
            "owner_kind": "service",
            "service_id": BROWSER_TASK_SERVICE_ID,
            "operator_owner_principal_id": owner_principal_id,
            "operator_owner_session_id": owner_session_id,
            # The browser worker is a service-owned durable job, but its
            # canonical goal remains owned by the operator that admitted the
            # Work Board task.  DurableJobRepository verifies this delegated
            # pair against the live Goal row before inserting the root.
            "goal_owner_principal_id": owner_principal_id,
            "goal_owner_session_id": owner_session_id,
            "goal_id": goal_id,
            "goal_revision": goal_revision,
            "board_task_revision": board_task_revision,
            "board_fencing_token": board_fencing_token,
            "priority": task_priority,
            # This immutable server-derived budget is the source of truth for
            # operator progress. A missing checkpoint must never become a
            # fabricated action count in the cockpit.
            "action_count": len(model.actions),
            "input_artifact_id": input_artifact_id,
            "input_artifact_digest": input_artifact_digest,
            "input_envelope_digest": input_envelope_digest,
            "browser_input_digest": input_model_digest,
            "action_consent_digest": action_consent_digest,
            "capability_id": BROWSER_TASK_CAPABILITY_ID,
            "capability_version": BROWSER_TASK_CAPABILITY_VERSION,
            "finite_authority": True,
            "budget_microusd": 0,
            "permissions": ["public_https_get_head", "workspace_artifact_write"],
            "limits": {
                "runtime_seconds": runtime_seconds,
                "max_attempts": max_attempts,
                "max_extract_bytes": BROWSER_MAX_EXTRACT_BYTES,
            },
        }
        if routine_parent_job_id:
            declared_authority["routine_parent_job_id"] = routine_parent_job_id
            declared_authority["routine_parent_fencing_token"] = int(routine_parent_fencing_token or 0)
            declared_authority["routine_step_id"] = routine_step_id
            # These values are copied from the server-validated procedure
            # child binding.  Durable admission compares them with the live
            # parent row before applying the native-child root budget
            # exemption; they are not caller-selectable browser inputs.
            declared_authority["routine_parent_goal_id"] = goal_id
            declared_authority["routine_parent_goal_revision"] = goal_revision
            declared_authority["routine_parent_owner_principal_id"] = owner_principal_id
            declared_authority["routine_parent_owner_session_id"] = owner_session_id
        if max_outstanding_jobs is not None:
            declared_authority["limits"]["max_outstanding_jobs"] = max_outstanding_jobs
        spec = DurableJobSpec(
            identity=DurableJobIdentity(
                job_id=job_id,
                owner_kind="service",
                owner_principal_id=BROWSER_TASK_OWNER_PRINCIPAL,
                job_kind=BROWSER_TASK_JOB_KIND,
                capability_version=BROWSER_TASK_CAPABILITY_VERSION,
                idempotency_scope="work-board-attempt",
                idempotency_key=f"{task_id}:{attempt_id}",
            ),
            inputs=safe_inputs,
            session_id=owner_session_id,
            conversation_id=owner_session_id,
            operator_session_id=owner_session_id,
            parent_job_id=routine_parent_job_id,
            parent_fencing_token=(
                int(routine_parent_fencing_token)
                if routine_parent_job_id is not None and routine_parent_fencing_token is not None
                else None
            ),
            goal_id=goal_id,
            goal_revision=goal_revision,
            priority=task_priority,
            resource_claims=("browser-task-context:global",),
            declared_authority=declared_authority,
            deadline_at=now + timedelta(seconds=runtime_seconds),
            max_attempts=max_attempts,
            max_outstanding_jobs=max_outstanding_jobs,
            service_id=BROWSER_TASK_SERVICE_ID,
            run_fingerprint=_digest(safe_inputs),
            budget_microusd=0,
        )
        admitted = await self.jobs.admit_job(spec)
        if not isinstance(admitted, Mapping) or _text(admitted.get("job_id")) != job_id:
            raise BrowserTaskError("durable admission identity mismatch", code="admission_identity_mismatch")
        durable_status = _text(admitted.get("status"), "accepted")
        return self._receipt(
            task_id=task_id,
            attempt_id=attempt_id,
            job_id=job_id,
            status="admitted",
            durable_status=durable_status,
            reason_code=None,
        )

    async def _execute(
        self,
        *,
        task_id: str,
        attempt_id: str,
        owner_principal_id: str,
        owner_session_id: str,
        goal_id: str | None,
        goal_revision: int | None,
        board_task_revision: int,
        board_fencing_token: int,
        admission_board_task_revision: int,
        task_priority: int,
        input_artifact_id: str,
        input_artifact_digest: str | None,
        input_envelope_digest: str,
        input_model_digest: str,
        action_consent_digest: str,
        model: BrowserTaskInput,
        runtime_seconds: int,
        job_id: str,
        durable_lease_owner: str | None,
        durable_fencing_token: int | None,
        max_attempts: int,
        max_outstanding_jobs: int | None,
        routine_parent_job_id: str | None = None,
        routine_parent_fencing_token: int | None = None,
    ) -> dict[str, Any]:
        current = await self.jobs.get_job(job_id)
        if not isinstance(current, Mapping):
            raise BrowserTaskError("durable root is missing", code="durable_root_missing")
        if _text(current.get("job_id")) != job_id:
            raise BrowserTaskError("durable root identity mismatch", code="durable_identity_mismatch")
        cleanup_binding = {
            "expected_job_id": job_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "owner_principal_id": owner_principal_id,
            "owner_session_id": owner_session_id,
            "goal_id": goal_id,
            "goal_revision": goal_revision,
            "board_task_revision": admission_board_task_revision,
            "board_fencing_token": board_fencing_token,
            "input_artifact_id": input_artifact_id,
            "input_artifact_digest": input_artifact_digest,
            "input_envelope_digest": input_envelope_digest,
            "input_model_digest": input_model_digest,
            "action_consent_digest": action_consent_digest,
            "action_count": len(model.actions),
            "task_priority": task_priority,
        }
        self._verify_durable_binding(current, **cleanup_binding)
        self._verify_effective_limits(
            current,
            max_attempts=max_attempts,
            max_outstanding_jobs=max_outstanding_jobs,
        )
        durable_status = _text(current.get("status"))
        if durable_status in {"succeeded", "cancelled", "degraded"}:
            replay_proof = {}
            if durable_status == "succeeded":
                replay_proof = self._terminal_replay_proof(
                    current,
                    expected_job_id=job_id,
                    workspace_root=self.workspace_root,
                )
                if replay_proof is None:
                    raise BrowserUnknownExternalEffect(
                        "terminal browser proof is incomplete; reconcile before replay",
                        code="terminal_replay_incomplete",
                    )
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                job_id=job_id,
                status="succeeded" if durable_status == "succeeded" else "blocked",
                durable_status=durable_status,
                reason_code="terminal_replay",
                **replay_proof,
            )
        if durable_status == "running":
            # A persisted running root may already have crossed the network
            # boundary.  Re-launching it after restart would duplicate an
            # external effect, so recovery must reconcile the existing root.
            raise BrowserUnknownExternalEffect(
                "durable browser root is already running; reconcile before retry",
                code="durable_running_reentry",
            )
        lease_owner = durable_lease_owner
        fencing_token = durable_fencing_token
        revision = int(current.get("revision") or 0)
        if durable_status == "accepted":
            current = await self.jobs.queue_job(job_id, expected_revision=revision)
            durable_status = _text(current.get("status"))
            revision = int(current.get("revision") or revision)
        if durable_status == "queued":
            lease_owner = lease_owner or f"{BROWSER_TASK_SERVICE_ID}:{attempt_id}"
            expected_fence = int((current.get("lease") or {}).get("fencing_token") or 0)
            current = await self.jobs.claim_job(
                job_id,
                owner=lease_owner,
                lease_seconds=runtime_seconds,
                expected_revision=revision,
                expected_fencing_token=expected_fence,
            )
            durable_status = _text(current.get("status"))
            revision = int(current.get("revision") or revision)
            fencing_token = int((current.get("lease") or {}).get("fencing_token") or 0)
        if durable_status != "running":
            raise BrowserTaskError(
                f"durable root is not runnable ({durable_status or 'unknown'})",
                code="durable_root_not_runnable",
            )
        lease_owner = lease_owner or _text((current.get("lease") or {}).get("owner"))
        fencing_token = fencing_token or int((current.get("lease") or {}).get("fencing_token") or 0)
        if not lease_owner or fencing_token <= 0:
            raise BrowserTaskError("durable lease is missing", code="durable_lease_missing")
        active = await self.jobs.assert_active_lease(
            job_id,
            owner=lease_owner,
            fencing_token=fencing_token,
        )
        revision = int(active.get("revision") or revision)
        execution_started = time.monotonic()
        try:
            durable_remaining = self._durable_remaining_seconds(active)
        except BrowserTaskError as exc:
            # Deadline validation happens before a launch resource holder is
            # entered.  Preserve the typed no-context proof so the dispatcher
            # releases the lane instead of quarantining a browser that never
            # existed.
            exc.cleanup_status = (
                "not_needed"
                if await self._record_prelaunch_cleanup_effect(
                    job_id=job_id,
                    lease_owner=lease_owner,
                    fencing_token=fencing_token,
                    binding=cleanup_binding,
                )
                else "cleanup_unknown"
            )
            raise
        if durable_remaining is None:
            durable_remaining = float(runtime_seconds)
        if durable_remaining <= 0:
            error = BrowserTaskError("durable browser deadline has expired", code="browser_runtime_deadline")
            error.cleanup_status = (
                "not_needed"
                if await self._record_prelaunch_cleanup_effect(
                    job_id=job_id,
                    lease_owner=lease_owner,
                    fencing_token=fencing_token,
                    binding=cleanup_binding,
                )
                else "cleanup_unknown"
            )
            raise error
        overall_runtime = min(float(runtime_seconds), durable_remaining)
        cleanup_reserve = min(
            float(BROWSER_CLEANUP_TIMEOUT_SECONDS),
            overall_runtime * BROWSER_CLEANUP_RESERVE_FRACTION,
        )
        state = _ExecutionState(
            task_id=task_id,
            attempt_id=attempt_id,
            job_id=job_id,
            owner_session_id=owner_session_id,
            owner_principal_id=owner_principal_id,
            goal_id=goal_id,
            goal_revision=goal_revision,
            board_task_revision=board_task_revision,
            admission_board_task_revision=admission_board_task_revision,
            board_fencing_token=board_fencing_token,
            input_artifact_id=input_artifact_id,
            input_artifact_digest=input_artifact_digest,
            input_envelope_digest=input_envelope_digest,
            input_model_digest=input_model_digest,
            action_consent_digest=action_consent_digest,
            action_count=len(model.actions),
            task_priority=task_priority,
            lease_owner=lease_owner,
            fencing_token=fencing_token,
            revision=revision,
            runtime_seconds=runtime_seconds,
            action_deadline_monotonic=execution_started + max(0.001, overall_runtime - cleanup_reserve),
            execution_deadline_monotonic=execution_started + overall_runtime,
        )
        transport = self.transport_factory()
        launch_resources = _BrowserLaunchResources()
        heartbeat_task: asyncio.Task[None] | None = None
        cleanup_status = "cleanup_unknown"
        cleanup_effect_recorded = False
        resources_closed = False
        try:
            if callable(getattr(self.jobs, "heartbeat_job", None)):
                heartbeat_task = asyncio.create_task(self._heartbeat_loop(state))
            await self._await_with_deadline(
                state,
                self._launch_session(launch_resources),
                phase="browser_launch",
            )
            page = await self._await_with_deadline(
                state,
                launch_resources.context.new_page(),
                phase="browser_new_page",
            )
            self._install_popup_and_download_guards(launch_resources.context, page, state)
            await self._await_with_deadline(
                state,
                transport.install_route_guard(
                    launch_resources.context,
                    allowed_hosts=model.allowed_hosts,
                    approved_url_prefixes=model.approved_url_prefixes,
                    before_request=lambda: self._before_request(state),
                    on_receipt=lambda receipt: self._on_request_receipt(state, receipt),
                ),
                phase="browser_route_guard",
            )
            await self._await_with_deadline(
                state,
                self._run_action(state, page, model, model.start_url, is_initial=True),
                phase="browser_initial_action",
            )
            self._assert_receipt_budget(state)
            for index, action in enumerate(model.actions):
                await self._await_with_deadline(
                    state,
                    self._run_action(state, page, model, action, index=index),
                    phase="browser_action",
                )
                self._assert_receipt_budget(state)
            self._assert_receipt_budget(state)
            await self._await_with_deadline(
                state,
                self._run_checks(state, page, model.final_expected_checks, action_index=None),
                phase="browser_final_checks",
            )
            self._assert_receipt_budget(state)
            if state.popup_blocked:
                raise BrowserVerificationError("popup creation is forbidden", code="popup_blocked")
            if state.download_blocked:
                raise BrowserVerificationError("downloads are forbidden", code="download_blocked")
            self._assert_receipt_budget(state)
            artifact_ref, artifact_sha, readback_id, verified_at = await self._await_with_deadline(
                state,
                self._write_and_readback(state, page, model),
                phase="browser_artifact_readback",
            )
            # Durable success is published only after the browser context and
            # its transport lane have been torn down within a bounded window.
            # A readback proves the artifact, while cleanup proves that this
            # execution no longer owns a live browser process.
            if heartbeat_task is not None:
                heartbeat_stopped = await self._cancel_task_bounded(
                    heartbeat_task,
                    timeout_seconds=self._cleanup_timeout_seconds(state),
                )
                heartbeat_task = None if heartbeat_stopped else heartbeat_task
                if not heartbeat_stopped:
                    raise BrowserUnknownExternalEffect(
                        "browser heartbeat did not stop within the cleanup budget",
                        code="browser_cleanup_failed",
                    )
            transport.cancel_pending_blocking()
            cleanup_ok = await self._close_launch_resources_bounded(
                launch_resources,
                timeout_seconds=self._cleanup_timeout_seconds(state),
            )
            resources_closed = cleanup_ok
            cleanup_status = "cleanup_verified" if cleanup_ok else "cleanup_unknown"
            state.cleanup_status = cleanup_status
            if not cleanup_ok:
                cleanup_effect_recorded = await self._record_cleanup_effect_bounded(
                    state,
                    cleanup_status=cleanup_status,
                    context_not_started=False,
                )
                raise BrowserUnknownExternalEffect(
                    "browser resources did not close cleanly after readback",
                    code="browser_cleanup_failed",
                )
            cleanup_effect_recorded = await self._record_cleanup_effect_bounded(
                state,
                cleanup_status=cleanup_status,
                context_not_started=False,
            )
            if not cleanup_effect_recorded:
                raise BrowserUnknownExternalEffect(
                    "browser cleanup proof could not be persisted",
                    code="browser_cleanup_receipt_failed",
                )
            # Cleanup itself can race cancellation, logout, goal revision, or
            # artifact revocation. Revalidate the same durable and board fence
            # immediately before the success CAS.
            self._assert_execution_budget(state)
            self._assert_receipt_budget(state)
            await self._await_with_deadline(
                state,
                self._assert_current(state),
                phase="browser_final_authority",
                reserve_cleanup=False,
            )
            self._assert_execution_budget(state)
            self._assert_receipt_budget(state)
            from src.guardian.opportunity_plans import stage_browser_plan_terminal
            opportunity_terminal_check = await stage_browser_plan_terminal(task_id, attempt_id)
            transition = await self._await_with_deadline(
                state,
                self.jobs.transition_job(
                    job_id,
                    "succeeded",
                    owner=state.lease_owner,
                    fencing_token=state.fencing_token,
                    expected_revision=state.revision,
                    terminal_authority_check=opportunity_terminal_check,
                    result_summary="public browser extraction verified by artifact readback",
                    result={
                        "cleanup_status": cleanup_status,
                        "cleanup_verified": cleanup_status == "cleanup_verified",
                        "memory_status": "no_learning",
                        "artifact_ref": artifact_ref,
                        "artifact_sha256": artifact_sha,
                        "readback_id": readback_id,
                        "verified_at": verified_at,
                    },
                ),
                phase="browser_success_transition",
                reserve_cleanup=False,
            )
            state.revision = int(transition.get("revision") or state.revision)
            actual_url = _text(getattr(page, "url", "")) or None
            safe_actual, actual_digest = _safe_receipt_url(actual_url)
            return self._receipt(
                task_id=task_id,
                attempt_id=attempt_id,
                job_id=job_id,
                status="succeeded",
                durable_status=_text(transition.get("status"), "succeeded"),
                action_count=len(model.actions),
                request_count=state.request_count,
                actual_page_url=safe_actual,
                actual_page_url_digest=actual_digest,
                action_receipts=state.action_receipts,
                request_receipts=state.request_receipts,
                checks=state.checks,
                artifact_ref=artifact_ref,
                artifact_sha256=artifact_sha,
                readback_id=readback_id,
                verified_at=verified_at,
                cleanup_status=cleanup_status,
            )
        except BrowserTaskError:
            raise
        except FileNotFoundError as exc:
            raise BrowserTaskError(
                "Chromium executable is unavailable",
                code="browser_unavailable",
                dispatched=False,
            ) from exc
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise BrowserUnknownExternalEffect(str(exc) or "browser action timed out", code="browser_timeout") from exc
        except Exception as exc:
            raise BrowserUnknownExternalEffect(type(exc).__name__, code="browser_execution_failed") from exc
        finally:
            if heartbeat_task is not None:
                stopped = await self._cancel_task_bounded(
                    heartbeat_task,
                    timeout_seconds=self._cleanup_timeout_seconds(state),
                )
                if stopped:
                    heartbeat_task = None
            if not resources_closed:
                cleanup_ok = await self._close_launch_resources_bounded(
                    launch_resources,
                    timeout_seconds=self._cleanup_timeout_seconds(state),
                )
                resources_closed = cleanup_ok
                if cleanup_ok:
                    cleanup_status = (
                        "not_needed"
                        if launch_resources.context_not_started
                        else "cleanup_verified"
                    )
                elif launch_resources.context_not_started:
                    cleanup_status = "not_needed"
                else:
                    cleanup_status = "cleanup_unknown"
                state.cleanup_status = cleanup_status
                active_error = sys.exc_info()[1]
                if isinstance(active_error, BrowserTaskError):
                    active_error.cleanup_status = cleanup_status
                if not cleanup_effect_recorded:
                    cleanup_effect_recorded = await self._record_cleanup_effect_bounded(
                        state,
                        cleanup_status=cleanup_status,
                        context_not_started=launch_resources.context_not_started,
                    )
            elif not cleanup_effect_recorded:
                state.cleanup_status = cleanup_status
                cleanup_effect_recorded = await self._record_cleanup_effect_bounded(
                    state,
                    cleanup_status=cleanup_status,
                    context_not_started=launch_resources.context_not_started,
                )
            with suppress(Exception):
                transport.cancel_pending_blocking()
            active_error = sys.exc_info()[1]
            if isinstance(active_error, BrowserTaskError):
                active_error.observed_request_receipts = [item for item in state.request_receipts
                    if type(item.get("status")) is int and 100 <= item["status"] <= 599]

    @staticmethod
    async def _close_session_bounded(
        session: _BrowserSession,
        *,
        timeout_seconds: float = BROWSER_CLEANUP_TIMEOUT_SECONDS,
    ) -> bool:
        if timeout_seconds <= 0:
            return False
        try:
            return bool(
                await asyncio.wait_for(
                    session.close(),
                    timeout=timeout_seconds,
                )
            )
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False

    async def _close_launch_resources_bounded(
        self,
        resources: _BrowserLaunchResources,
        *,
        timeout_seconds: float = BROWSER_CLEANUP_TIMEOUT_SECONDS,
    ) -> bool:
        """Close every handle known from a launch, without extending the deadline."""

        if resources.cleanup_result is not None:
            return resources.cleanup_result
        if not resources.has_resources:
            # A call that never entered launch is the only proven no-context
            # outcome.  A launcher that was entered but returned no handle is
            # an unknown external boundary and must quarantine the lane.
            result = not resources.launch_attempted
            resources.cleanup_result = result
            return result
        if timeout_seconds <= 0:
            return False
        if resources.cleanup_task is None:
            resources.cleanup_task = asyncio.create_task(resources.close())
        try:
            await asyncio.wait_for(asyncio.shield(resources.cleanup_task), timeout=timeout_seconds)
        except asyncio.CancelledError:
            # The shielded cleanup task remains owned by the holder.  The
            # caller's cancellation is preserved by returning an unverified
            # outcome to its finally block.
            return False
        except (asyncio.TimeoutError, TimeoutError):
            return False
        except BaseException:
            resources.cleanup_result = False
            return False
        if not resources.cleanup_task.done():
            return False
        try:
            resources.cleanup_result = bool(resources.cleanup_task.result())
        except BaseException:
            resources.cleanup_result = False
        return resources.cleanup_result

    @staticmethod
    async def _cancel_task_bounded(
        task: asyncio.Task[Any],
        *,
        timeout_seconds: float,
    ) -> bool:
        caller = asyncio.current_task()
        cancelling_before = caller.cancelling() if caller is not None else 0
        if task.done():
            with suppress(asyncio.CancelledError, Exception):
                task.result()
            return True
        task.cancel()
        if timeout_seconds <= 0:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except (asyncio.CancelledError, asyncio.TimeoutError, TimeoutError):
            # Cancelling the child itself is the normal heartbeat teardown
            # path: ``shield`` reports its CancelledError even though the
            # child is already finished.  Do not turn that clean cancellation
            # into browser_cleanup_failed.  A new cancellation count on the
            # caller, however, belongs to the parent and must propagate rather
            # than being swallowed by cleanup.
            if caller is not None and caller.cancelling() > cancelling_before:
                raise
            if task.done():
                return True
            return False
        except Exception:
            return True
        return task.done()

    @classmethod
    def _remaining_async_seconds(cls, state: "_ExecutionState", *, reserve_cleanup: bool) -> float:
        deadline = (
            state.action_deadline_monotonic
            if reserve_cleanup
            else state.execution_deadline_monotonic
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise cls._runtime_deadline_error(state)
        return remaining

    async def _await_with_deadline(
        self,
        state: "_ExecutionState",
        operation: Awaitable[Any],
        *,
        phase: str,
        reserve_cleanup: bool = True,
    ) -> Any:
        """Bound one async boundary to the same durable execution deadline."""

        try:
            timeout_seconds = self._remaining_async_seconds(
                state,
                reserve_cleanup=reserve_cleanup,
            )
        except BrowserTaskError:
            if inspect.iscoroutine(operation):
                operation.close()
            raise
        try:
            return await asyncio.wait_for(operation, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise self._runtime_deadline_error(state) from exc
        except TimeoutError as exc:
            raise self._runtime_deadline_error(state) from exc

    async def _record_cleanup_effect_bounded(
        self,
        state: "_ExecutionState",
        *,
        cleanup_status: str,
        context_not_started: bool,
    ) -> bool:
        try:
            return bool(
                await self._await_with_deadline(
                    state,
                    self._record_cleanup_effect(
                        state,
                        cleanup_status=cleanup_status,
                        context_not_started=context_not_started,
                    ),
                    phase="cleanup_receipt",
                    reserve_cleanup=False,
                )
            )
        except Exception:
            return False

    async def _record_prelaunch_cleanup_effect(
        self,
        *,
        job_id: str,
        lease_owner: str,
        fencing_token: int,
        binding: Mapping[str, Any],
    ) -> bool:
        """Persist the no-context proof for a claimed root that never launched."""

        recorder = getattr(self.jobs, "record_effect", None)
        if not callable(recorder):
            # Lightweight runner doubles do not own a durable effect ledger;
            # the absence of a recorder cannot imply a browser was launched.
            return True

        async def record() -> None:
            revision = await self._refresh_cleanup_revision(
                job_id=job_id, lease_owner=lease_owner,
                fencing_token=fencing_token, binding=binding,
            )
            await recorder(
                job_id,
                effect_id=f"browser-cleanup:{job_id}",
                effect_type="browser_context_cleanup",
                status="succeeded",
                details={
                    "cleanup_status": "not_needed",
                    "context_not_started": True,
                    "memory_status": "no_learning",
                },
                receipt_kind="effect",
                owner=lease_owner,
                fencing_token=fencing_token,
                expected_revision=revision,
            )

        try:
            await asyncio.wait_for(record(), timeout=BROWSER_CLEANUP_TIMEOUT_SECONDS)
            return True
        except Exception:
            return False

    @staticmethod
    def _durable_remaining_seconds(projection: Mapping[str, Any]) -> float | None:
        """Return the remaining wall-clock deadline from the durable root.

        Test doubles and legacy rows may omit ``deadline_at``; the execution
        budget still remains bounded by the server-supplied runtime in that
        case. A malformed explicit deadline is treated as expired rather than
        allowing an unbounded browser operation.
        """

        raw = _text(projection.get("deadline_at"))
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise BrowserTaskError(
                "durable browser deadline is malformed",
                code="browser_runtime_deadline",
            ) from exc
        # SQLite returns the timezone-aware admission datetime as an ISO
        # timestamp without its offset.  The durable repository normalizes
        # that form as UTC; mirror the same rule at the runner boundary while
        # continuing to reject malformed explicit values above.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed - datetime.now(timezone.utc)).total_seconds()

    @staticmethod
    def _cleanup_timeout_seconds(state: "_ExecutionState") -> float:
        return max(0.0, min(
            float(BROWSER_CLEANUP_TIMEOUT_SECONDS),
            state.execution_deadline_monotonic - time.monotonic(),
        ))

    @staticmethod
    def _runtime_deadline_error(state: "_ExecutionState") -> BrowserTaskError:
        error_type = BrowserUnknownExternalEffect if state.network_dispatched else BrowserTaskError
        return error_type(
            "browser execution budget expired before the next bounded operation",
            code="browser_runtime_deadline",
        )

    @classmethod
    def _assert_execution_budget(cls, state: "_ExecutionState") -> None:
        if time.monotonic() >= state.execution_deadline_monotonic:
            raise cls._runtime_deadline_error(state)

    @classmethod
    def _operation_timeout_ms(cls, state: "_ExecutionState") -> int:
        remaining = state.action_deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise cls._runtime_deadline_error(state)
        return max(1, int(remaining * 1000))

    @classmethod
    async def _locator_value(
        cls,
        state: "_ExecutionState",
        locator: Any,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        timeout_ms = cls._operation_timeout_ms(state)
        method = getattr(locator, method_name)
        try:
            value = await method(*args, timeout=timeout_ms, **kwargs)
        except TypeError as exc:
            # Provider-free test doubles predate Playwright's timeout keyword;
            # the real Locator API always accepts it. Keep those doubles
            # usable without weakening the production bounded call.
            if "unexpected keyword argument 'timeout'" not in str(exc):
                raise
            value = await method(*args, **kwargs)
        cls._assert_execution_budget(state)
        return value

    async def _refresh_cleanup_revision(
        self, *, job_id: str, lease_owner: str, fencing_token: int,
        binding: Mapping[str, Any],
    ) -> int:
        """Refresh native cleanup CAS authority without authorizing execution."""
        projection = await self.jobs.assert_active_lease(
            job_id, owner=lease_owner, fencing_token=fencing_token,
        )
        self._verify_durable_binding(projection, **binding)
        return int(projection["revision"])

    @staticmethod
    def _cleanup_binding(state: "_ExecutionState") -> dict[str, Any]:
        return {
            "expected_job_id": state.job_id,
            "task_id": state.task_id,
            "attempt_id": state.attempt_id,
            "owner_principal_id": state.owner_principal_id,
            "owner_session_id": state.owner_session_id,
            "goal_id": state.goal_id,
            "goal_revision": state.goal_revision,
            "input_artifact_id": state.input_artifact_id,
            "input_artifact_digest": state.input_artifact_digest,
            "input_envelope_digest": state.input_envelope_digest,
            "input_model_digest": state.input_model_digest,
            "action_consent_digest": state.action_consent_digest,
            "action_count": state.action_count,
            "board_task_revision": state.admission_board_task_revision,
            "board_fencing_token": state.board_fencing_token,
            "task_priority": state.task_priority,
        }

    async def _record_cleanup_effect(
        self,
        state: "_ExecutionState",
        *,
        cleanup_status: str,
        context_not_started: bool,
    ) -> bool:
        """Persist the bounded browser-resource outcome before final CAS."""

        recorder = getattr(self.jobs, "record_effect", None)
        if not callable(recorder):
            # Lightweight runner doubles do not own a durable effect ledger;
            # the real repository always does.  Their lifecycle is still
            # covered by the runner receipt tests.
            return True
        status = "succeeded" if cleanup_status in {"cleanup_verified", "not_needed"} else "unknown"
        try:
            state.revision = await self._refresh_cleanup_revision(
                job_id=state.job_id, lease_owner=state.lease_owner,
                fencing_token=state.fencing_token, binding=self._cleanup_binding(state),
            )
            receipt = await recorder(
                state.job_id,
                effect_id=f"browser-cleanup:{state.job_id}",
                effect_type="browser_context_cleanup",
                status=status,
                details={
                    "cleanup_status": cleanup_status,
                    "context_not_started": bool(context_not_started),
                    "memory_status": "no_learning",
                },
                receipt_kind="effect",
                owner=state.lease_owner,
                fencing_token=state.fencing_token,
                expected_revision=state.revision,
            )
            if isinstance(receipt, Mapping):
                state.revision = int(receipt.get("revision") or state.revision)
            return True
        except Exception:
            return False

    async def _heartbeat_loop(self, state: "_ExecutionState") -> None:
        """Keep the durable lease live while Chromium is inside one action."""

        interval = max(1.0, min(30.0, state.runtime_seconds / 3))
        heartbeat = getattr(self.jobs, "heartbeat_job", None)
        if heartbeat is None:
            return
        while True:
            await asyncio.sleep(interval)
            try:
                async with state.lock:
                    refreshed = await heartbeat(
                        state.job_id,
                        owner=state.lease_owner,
                        fencing_token=state.fencing_token,
                        lease_seconds=state.runtime_seconds,
                        expected_revision=state.revision,
                        expected_fencing_token=state.fencing_token,
                    )
                    state.revision = int(refreshed.get("revision") or state.revision)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.heartbeat_error = type(exc).__name__
                return

    async def _launch_session(self, resources: _BrowserLaunchResources) -> _BrowserSession:
        options = {
            "java_script_enabled": False,
            "accept_downloads": False,
            "service_workers": "block",
            "storage_state": None,
            "http_credentials": None,
            "permissions": [],
        }
        if self.browser_launcher is not None:
            resources.launch_attempted = True
            browser = self.browser_launcher()
            if inspect.isawaitable(browser):
                browser = await browser
            resources.browser = browser
            context = await browser.new_context(**options)
            resources.context = context
            resources.session = _BrowserSession(browser=browser, context=context)
            return resources.session
        from playwright.async_api import async_playwright

        if not _playwright_browser_executable_present():
            raise BrowserTaskError("Playwright browser executable is unavailable", code="browser_runtime_unavailable")
        # Imports and local file preflight are proven pre-child failures. Only
        # entering the driver/launcher crosses the unknown resource boundary.
        resources.launch_attempted = True
        playwright = await async_playwright().start()
        resources.playwright = playwright
        browser = await playwright.chromium.launch(headless=True)
        resources.browser = browser
        context = await browser.new_context(**options)
        resources.context = context
        resources.session = _BrowserSession(browser=browser, context=context, playwright=playwright)
        return resources.session

    @staticmethod
    def _install_popup_and_download_guards(context: Any, page: Any, state: "_ExecutionState") -> None:
        def on_popup(new_page: Any) -> None:
            if new_page is page:
                return
            state.popup_blocked = True
            close = getattr(new_page, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    asyncio.create_task(result)

        def on_download(download: Any) -> None:
            state.download_blocked = True
            cancel = getattr(download, "cancel", None)
            if cancel is not None:
                result = cancel()
                if inspect.isawaitable(result):
                    asyncio.create_task(result)

        context_on = getattr(context, "on", None)
        if context_on is not None:
            context_on("page", on_popup)
        page_on = getattr(page, "on", None)
        if page_on is not None:
            page_on("download", on_download)

    async def _before_request(self, state: "_ExecutionState") -> None:
        # Cheaply reject a request after the bounded receipt/dispatch budget is
        # latched.  In particular, a page may emit blocked resource requests
        # without ever reaching the transport's before-request callback; no
        # extra durable authority read is useful once this execution is done.
        async with state.lock:
            if state.receipt_limit_hit:
                raise self._receipt_limit_error(state)
            if state.request_dispatches >= BROWSER_MAX_REQUESTS:
                self._mark_receipt_limit(state, "request_limit")
                raise self._receipt_limit_error(state)
            if state.request_receipt_events >= BROWSER_MAX_REQUESTS:
                self._mark_receipt_limit(state, "receipt_limit")
                raise self._receipt_limit_error(state)
        await self._assert_current(state)
        async with state.lock:
            # Authority revalidation can yield while a blocked-resource
            # callback reaches the same state. Recheck the monotonic latch and
            # both bounded counters before recording a dispatch checkpoint.
            if state.receipt_limit_hit:
                raise self._receipt_limit_error(state)
            if state.request_dispatches >= BROWSER_MAX_REQUESTS:
                self._mark_receipt_limit(state, "request_limit")
                raise self._receipt_limit_error(state)
            if state.request_receipt_events >= BROWSER_MAX_REQUESTS:
                self._mark_receipt_limit(state, "receipt_limit")
                raise self._receipt_limit_error(state)
            # Every admitted subrequest repeats the canonical dependency
            # guard in the same native checkpoint transaction. Updating the
            # stable marker retains dispatch truth within the existing cap:
            # at most 32 progress markers plus nine action markers remain.
            await self._checkpoint(
                state,
                checkpoint_id="network-dispatch",
                payload={
                    "phase": "network_dispatch",
                    "request_count": state.request_dispatches,
                    "request_dispatch_count": state.request_dispatches + 1,
                    "action_index": state.current_action_index,
                },
            )
            state.network_checkpointed = True
            state.request_dispatches += 1
            state.network_dispatched = True

    async def _on_request_receipt(self, state: "_ExecutionState", receipt: Mapping[str, Any]) -> None:
        async with state.lock:
            # Route callbacks can arrive for method/resource types rejected
            # before ``before_request``. Treat every callback as one bounded
            # receipt event, but return before another durable write once the
            # cap is exhausted. The latch is monotonic so a transport handler
            # that invokes this callback again while aborting cannot duplicate
            # the limit handling or checkpoint flood.
            if state.receipt_limit_hit:
                return
            if (
                state.request_receipt_events >= BROWSER_MAX_REQUESTS
                or len(state.request_receipts) >= BROWSER_MAX_REQUESTS
            ):
                self._mark_receipt_limit(state, "receipt_limit")
                return
            state.request_receipts.append(self._safe_request_receipt(receipt))
            state.request_receipt_events += 1
            state.request_count = len(state.request_receipts)
            try:
                await self._checkpoint(
                    state,
                    checkpoint_id=f"network-progress-{state.request_count}",
                    payload={
                        "phase": "network_progress",
                        "request_count": sum(type(item.get("status")) is int
                            and 100 <= item["status"] <= 599 for item in state.request_receipts),
                        "action_index": state.current_action_index,
                    },
                )
            except Exception as exc:
                from src.work_board.repository import BoardError
                if isinstance(exc, BoardError) and exc.code == "evidence_dependency_stale":
                    # A correction can follow admitted contact. Preserve the
                    # actual callback as observation only; the rejected guard
                    # still terminates use. This existing effect path enforces
                    # the original Goal, deadline, lease, revision and fence.
                    recorded = await self.jobs.record_effect(
                        state.job_id,
                        effect_id=f"browser-network-observation:{state.job_id}:{state.request_count}",
                        effect_type="browser_network_observation",
                        status="succeeded",
                        details={
                            "observation_only": True,
                            "request_count": state.request_count,
                            "request_dispatch_count": state.request_dispatches,
                            "request_receipt": state.request_receipts[-1],
                            "memory_status": "no_learning",
                        },
                        owner=state.lease_owner,
                        fencing_token=state.fencing_token,
                        expected_revision=state.revision,
                    )
                    state.revision = int(recorded.get("revision") or state.revision)
                raise

    @staticmethod
    def _mark_receipt_limit(state: "_ExecutionState", code: str) -> None:
        if not state.receipt_limit_hit:
            state.receipt_limit_hit = True
            state.receipt_limit_code = code

    @staticmethod
    def _receipt_limit_error(state: "_ExecutionState") -> BrowserTaskError:
        error_type = BrowserUnknownExternalEffect if state.network_dispatched else BrowserTaskError
        return error_type(
            "browser request receipt limit exceeded",
            code=state.receipt_limit_code or "receipt_limit",
        )

    @classmethod
    def _assert_receipt_budget(cls, state: "_ExecutionState") -> None:
        if state.receipt_limit_hit:
            raise cls._receipt_limit_error(state)

    async def _assert_current(self, state: "_ExecutionState") -> Mapping[str, Any]:
        # Lease snapshots and revision writes share the checkpoint/heartbeat
        # boundary so a delayed read cannot replace a newer local revision.
        async with state.lock:
            if state.heartbeat_error:
                raise BrowserTaskError(
                    "durable lease heartbeat failed",
                    code="durable_heartbeat_failed",
                    dispatched=state.network_dispatched,
                )
            projection = await self.jobs.assert_active_lease(
                state.job_id,
                owner=state.lease_owner,
                fencing_token=state.fencing_token,
            )
            self._verify_durable_binding(
                projection,
                expected_job_id=state.job_id,
                task_id=state.task_id,
                attempt_id=state.attempt_id,
                owner_principal_id=state.owner_principal_id,
                owner_session_id=state.owner_session_id,
                goal_id=state.goal_id,
                goal_revision=state.goal_revision,
                input_artifact_id=state.input_artifact_id,
                input_artifact_digest=state.input_artifact_digest,
                input_envelope_digest=state.input_envelope_digest,
                input_model_digest=state.input_model_digest,
                action_consent_digest=state.action_consent_digest,
                action_count=state.action_count,
                board_task_revision=state.admission_board_task_revision,
                board_fencing_token=state.board_fencing_token,
                task_priority=state.task_priority,
            )
            state.revision = int(projection.get("revision") or state.revision)
        control = self.runtime_controls
        if control is not None:
            callback = getattr(control, "assert_current", None)
            if callback is None and callable(control):
                callback = control
            if callback is None:
                raise BrowserTaskError("runtime control is invalid", code="runtime_control_invalid")
            result = callback(
                task_id=state.task_id,
                attempt_id=state.attempt_id,
                board_task_revision=state.board_task_revision,
                board_fencing_token=state.board_fencing_token,
                owner_principal_id=state.owner_principal_id,
                owner_session_id=state.owner_session_id,
                goal_id=state.goal_id,
                goal_revision=state.goal_revision,
                input_artifact_id=state.input_artifact_id,
                durable_job_id=state.job_id,
            )
            if inspect.isawaitable(result):
                result = await result
            if result is False:
                raise BrowserTaskError("Work Board authority is stale", code="board_fence_stale")
        return projection

    async def _checkpoint(self, state: "_ExecutionState", *, checkpoint_id: str, payload: Mapping[str, Any]) -> None:
        recorded = await self.jobs.record_checkpoint(
            state.job_id,
            checkpoint_id=checkpoint_id,
            state={"checkpoint_id": checkpoint_id, **dict(payload)},
            # Browser progress is bounded and capability-approved. Retain it
            # for the operator projection; other jobs still pass no payload.
            checkpoint_payload=dict(payload),
            owner=state.lease_owner,
            fencing_token=state.fencing_token,
            expected_revision=state.revision,
        )
        state.revision = int(recorded.get("revision") or state.revision)

    async def _run_action(
        self,
        state: "_ExecutionState",
        page: Any,
        model: BrowserTaskInput,
        action: BrowserAction | str,
        *,
        index: int | None = None,
        is_initial: bool = False,
    ) -> None:
        self._assert_receipt_budget(state)
        if is_initial:
            url = str(action)
            action_digest = _digest({"kind": "initial_navigate", "url": url})
            action_index = -1
            kind = "navigate"
        else:
            assert isinstance(action, BrowserAction)
            url = action.url if action.kind == "navigate" else None
            action_digest = _digest(action.model_dump(mode="json"))
            action_index = int(index or 0)
            kind = action.kind
        state.current_action_index = action_index
        await self._assert_current(state)
        self._assert_execution_budget(state)
        async with state.lock:
            await self._checkpoint(
                state,
                checkpoint_id=f"action-{action_index}-pre-dispatch",
                payload={
                    "action_index": action_index,
                    "action_digest": action_digest,
                    "phase": "pre_dispatch",
                    "context_revision": state.context_revision,
                    "request_count": state.request_count,
                },
            )
        if kind == "navigate":
            state.navigation_count += 1
            if state.navigation_count > BROWSER_MAX_NAVIGATIONS:
                raise BrowserTaskError("navigation limit exceeded", code="navigation_limit")
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._operation_timeout_ms(state),
                )
            except Exception as exc:
                if state.receipt_limit_hit:
                    self._assert_receipt_budget(state)
                if time.monotonic() >= state.action_deadline_monotonic:
                    raise self._runtime_deadline_error(state) from exc
                if state.network_dispatched:
                    raise BrowserUnknownExternalEffect(type(exc).__name__, code="navigation_failed") from exc
                raise BrowserTaskError(type(exc).__name__, code="navigation_blocked") from exc
            if response is not None and int(getattr(response, "status", 200) or 200) >= 400:
                raise BrowserVerificationError("navigation returned an error status", code="navigation_http_error")
            self._assert_page_url_consented(page, model)
            if not is_initial:
                assert isinstance(action, BrowserAction)
                await self._run_checks(state, page, action.expected_checks, action_index=action_index)
                state.action_receipts.append(
                    {
                        "action_index": action_index,
                        "kind": "navigate",
                        "url": _safe_receipt_url(url)[0],
                        "url_digest": _safe_receipt_url(url)[1],
                        "navigation_count": state.navigation_count,
                    }
                )
        else:
            assert isinstance(action, BrowserAction)
            self._assert_page_url_consented(page, model)
            locator = page.locator(action.selector).first
            try:
                if action.attribute is None:
                    value = await self._locator_value(state, locator, "inner_text")
                else:
                    value = await self._locator_value(state, locator, "get_attribute", action.attribute)
                    value = value or ""
            except (asyncio.TimeoutError, TimeoutError) as exc:
                if time.monotonic() >= state.action_deadline_monotonic:
                    raise self._runtime_deadline_error(state) from exc
                raise BrowserUnknownExternalEffect(
                    f"browser extraction timed out at action {action_index} "
                    f"(selector_digest={_digest(action.selector)})",
                    code="extract_selector_timeout",
                ) from exc
            except Exception as exc:
                if time.monotonic() >= state.action_deadline_monotonic:
                    raise self._runtime_deadline_error(state) from exc
                # Playwright exposes its own TimeoutError class. Keep the
                # operator receipt typed and bounded without importing a
                # browser-only exception into the provider-free path.
                if type(exc).__name__ == "TimeoutError":
                    raise BrowserUnknownExternalEffect(
                        f"browser extraction timed out at action {action_index} "
                        f"(selector_digest={_digest(action.selector)})",
                        code="extract_selector_timeout",
                    ) from exc
                raise
            if not isinstance(value, str):
                value = str(value)
            if len(value) > action.max_chars:
                value = value[: action.max_chars]
            state.extracts.append(
                {
                    "action_index": action_index,
                    "kind": "extract",
                    "selector": action.selector,
                    "attribute": action.attribute,
                    "value": value,
                }
            )
            await self._ensure_extract_bound(state)
            self._assert_page_url_consented(page, model)
            await self._run_checks(state, page, action.expected_checks, action_index=action_index)
            state.action_receipts.append(
                {
                    "action_index": action_index,
                    "kind": "extract",
                    "selector_digest": _digest(action.selector),
                    "attribute": action.attribute,
                    "chars": len(value),
                    "value_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                }
            )

    @staticmethod
    def _assert_page_url_consented(page: Any, model: BrowserTaskInput) -> None:
        """Keep the observed Chromium URL inside the same explicit consent."""

        actual_url = _text(getattr(page, "url", ""))
        try:
            if not any(_prefix_matches(actual_url, prefix) for prefix in model.approved_url_prefixes):
                raise BrowserVerificationError(
                    "current browser URL is outside approved prefixes",
                    code="page_url_not_consented",
                )
        except BrowserVerificationError:
            raise
        except Exception as exc:
            raise BrowserVerificationError(
                "current browser URL is invalid",
                code="page_url_invalid",
            ) from exc

    async def _run_checks(
        self,
        state: "_ExecutionState",
        page: Any,
        checks: list[BrowserExpectedCheck],
        *,
        action_index: int | None,
    ) -> None:
        for check in checks:
            self._assert_execution_budget(state)
            passed = False
            actual_digest: str | None = None
            try:
                if check.kind == "url_host":
                    passed = (_text(urlsplit(str(getattr(page, "url", ""))).hostname).lower() == check.value.lower())
                elif check.kind == "url_path_prefix":
                    passed = _path_prefix_match(urlsplit(str(getattr(page, "url", ""))).path, check.value)
                else:
                    locator = page.locator(check.selector or "").first
                    value = await self._locator_value(state, locator, "inner_text")
                    actual_digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
                    passed = check.value in value if check.kind == "text_contains" else actual_digest == check.value
            except BrowserTaskError:
                raise
            except Exception:
                passed = False
            receipt = {
                "action_index": action_index,
                "kind": check.kind,
                "selector_digest": _digest(check.selector) if check.selector else None,
                "expected_digest": hashlib.sha256(check.value.encode("utf-8")).hexdigest(),
                "actual_digest": actual_digest,
                "passed": passed,
            }
            state.checks.append(receipt)
            if not passed:
                raise BrowserVerificationError("expected browser check failed", code="expected_check_failed")

    async def _ensure_extract_bound(self, state: "_ExecutionState") -> None:
        content = _canonical_json(state.extracts).encode("utf-8")
        if len(content) > BROWSER_MAX_EXTRACT_BYTES:
            raise BrowserVerificationError("aggregate extract output exceeds 64 KiB", code="extract_output_limit")

    async def _write_and_readback(
        self,
        state: "_ExecutionState",
        page: Any,
        model: BrowserTaskInput,
    ) -> tuple[str, str, str, str]:
        self._assert_receipt_budget(state)
        await self._assert_current(state)
        self._assert_execution_budget(state)
        self._assert_receipt_budget(state)
        payload = {
            "schema_version": 1,
            "capability_id": BROWSER_TASK_CAPABILITY_ID,
            "task_id": state.task_id,
            "attempt_id": state.attempt_id,
            "final_url": _safe_receipt_url(_text(getattr(page, "url", "")))[0],
            "extracts": state.extracts,
            "checks": state.checks,
            "request_count": state.request_count,
        }
        content = _canonical_json(payload).encode("utf-8")
        if len(content) > BROWSER_MAX_EXTRACT_BYTES:
            raise BrowserVerificationError("serialized browser artifact exceeds 64 KiB", code="artifact_output_limit")
        digest = hashlib.sha256(content).hexdigest()
        relative = browser_artifact_path_for_job(state.job_id)
        try:
            _write_browser_artifact_bytes(
                relative,
                content,
                workspace_root=self.workspace_root,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise BrowserVerificationError(
                "browser artifact write failed",
                code="artifact_write_failed",
            ) from exc
        readback = read_browser_artifact_bytes(
            relative,
            workspace_root=self.workspace_root,
            max_bytes=BROWSER_MAX_EXTRACT_BYTES,
        )
        if readback is None:
            raise BrowserVerificationError(
                "browser artifact readback unavailable",
                code="artifact_readback_unavailable",
            )
        if readback != content or hashlib.sha256(readback).hexdigest() != digest:
            raise BrowserVerificationError("browser artifact readback digest mismatch", code="artifact_readback_mismatch")
        self._assert_receipt_budget(state)
        async with state.lock:
            artifact = await self.jobs.record_artifact(
                state.job_id,
                file_path=relative,
                artifact_type="browser_public_task_result",
                content=readback,
                owner=state.lease_owner,
                fencing_token=state.fencing_token,
                expected_revision=state.revision,
            )
            state.revision = int(artifact.get("revision") or state.revision)
        self._assert_receipt_budget(state)
        verified_at = datetime.now(timezone.utc).isoformat()
        readback_id = "readback-" + _digest(
            {"job_id": state.job_id, "path": relative, "digest": digest}
        )[:32]
        async with state.lock:
            receipt = await self.jobs.record_readback(
                state.job_id,
                target_path=relative,
                effect_type="browser_public_task_result",
                target_digest=digest,
                content_sha256=digest,
                readback_id=readback_id,
                verified_at=verified_at,
                status="succeeded",
                details={
                    "verified": True,
                    "size_bytes": len(readback),
                    "request_count": state.request_count,
                    "action_count": len(model.actions),
                },
                owner=state.lease_owner,
                fencing_token=state.fencing_token,
                expected_revision=state.revision,
            )
            state.revision = int(receipt.get("revision") or state.revision)
        return relative, digest, readback_id, verified_at

    def _verify_durable_binding(
        self,
        projection: Mapping[str, Any],
        *,
        expected_job_id: str | None = None,
        task_id: str,
        attempt_id: str,
        owner_principal_id: str,
        owner_session_id: str,
        goal_id: str | None,
        goal_revision: int | None,
        input_artifact_id: str,
        input_artifact_digest: str | None,
        board_task_revision: int,
        board_fencing_token: int,
        task_priority: int,
        input_envelope_digest: str,
        input_model_digest: str,
        action_consent_digest: str,
        action_count: int,
    ) -> None:
        if not goal_id or type(goal_revision) is not int or goal_revision < 1:
            raise BrowserTaskError("durable goal binding is incomplete", code="durable_goal_mismatch")
        if not input_artifact_digest:
            raise BrowserTaskError("durable artifact binding is incomplete", code="durable_artifact_mismatch")
        if type(action_count) is not int or not 1 <= action_count <= BROWSER_MAX_ACTIONS:
            raise BrowserTaskError("durable action budget is incomplete", code="durable_action_count_invalid")
        bound_job_id = _text(expected_job_id) or f"browser-task:{task_id}:{attempt_id}"
        if _text(projection.get("job_id")) != bound_job_id:
            raise BrowserTaskError("durable job identity is not bound to task attempt", code="durable_identity_mismatch")
        if _text(projection.get("session_id")) != owner_session_id:
            raise BrowserTaskError("durable session binding is stale", code="durable_session_mismatch")
        if _text(projection.get("operator_session_id")) != owner_session_id:
            raise BrowserTaskError("durable operator session binding is stale", code="durable_session_mismatch")
        if projection.get("goal_id") != goal_id or projection.get("goal_revision") != goal_revision:
            raise BrowserTaskError("durable goal binding is stale", code="durable_goal_mismatch")
        authority = projection.get("declared_authority")
        owner = projection.get("owner")
        if not isinstance(authority, Mapping) or not isinstance(owner, Mapping):
            raise BrowserTaskError("durable authority binding is missing", code="durable_authority_missing")
        routine_parent_id = _text(authority.get("routine_parent_job_id"))
        if routine_parent_id:
            try:
                routine_parent_fence = int(authority.get("routine_parent_fencing_token") or 0)
                projected_parent_fence = int(projection.get("parent_fencing_token") or 0)
            except (TypeError, ValueError) as exc:
                raise BrowserTaskError("durable procedure parent binding is malformed", code="durable_parent_binding_stale") from exc
            if (
                _text(projection.get("parent_job_id")) != routine_parent_id
                or _text(projection.get("parent_run_identity")) != routine_parent_id
                or _text(projection.get("root_run_identity")) != routine_parent_id
                or projected_parent_fence != routine_parent_fence
                or routine_parent_fence <= 0
                or _text(authority.get("routine_step_id")) != "public_browser_check"
            ):
                raise BrowserTaskError("durable procedure parent binding is stale", code="durable_parent_binding_stale")
        if (
            _text(owner.get("kind")) != "service"
            or _text(owner.get("principal_id")) != BROWSER_TASK_OWNER_PRINCIPAL
            or _text(owner.get("service_id")) != BROWSER_TASK_SERVICE_ID
            or _text(projection.get("job_kind")) != BROWSER_TASK_JOB_KIND
            or _text(projection.get("capability_version")) != BROWSER_TASK_CAPABILITY_VERSION
            or _text(authority.get("principal")) != BROWSER_TASK_OWNER_PRINCIPAL
            or _text(authority.get("owner_kind")) != "service"
            or _text(authority.get("service_id")) != BROWSER_TASK_SERVICE_ID
            or _text(authority.get("capability_id")) != BROWSER_TASK_CAPABILITY_ID
            or _text(authority.get("capability_version")) != BROWSER_TASK_CAPABILITY_VERSION
        ):
            raise BrowserTaskError("durable capability binding is stale", code="durable_capability_mismatch")
        if (
            _text(authority.get("operator_owner_principal_id")) != owner_principal_id
            or _text(authority.get("operator_owner_session_id")) != owner_session_id
            or _text(authority.get("goal_owner_principal_id")) != owner_principal_id
            or _text(authority.get("goal_owner_session_id")) != owner_session_id
            or authority.get("goal_id") != goal_id
            or authority.get("goal_revision") != goal_revision
        ):
            raise BrowserTaskError("durable owner binding is stale", code="durable_owner_mismatch")
        if (
            authority.get("board_task_revision") != board_task_revision
            or authority.get("board_fencing_token") != board_fencing_token
        ):
            raise BrowserTaskError("durable board admission binding is stale", code="durable_board_binding_stale")
        if (
            _text(authority.get("input_artifact_id")) != input_artifact_id
            or (authority.get("input_artifact_digest") or None) != (input_artifact_digest or None)
            or authority.get("input_envelope_digest") != input_envelope_digest
            or authority.get("browser_input_digest") != input_model_digest
            or authority.get("action_consent_digest") != action_consent_digest
            or type(authority.get("action_count")) is not int
            or authority.get("action_count") != action_count
        ):
            raise BrowserTaskError("durable artifact binding is stale", code="durable_artifact_mismatch")
        if authority.get("priority") != task_priority:
            raise BrowserTaskError("durable priority binding is stale", code="durable_priority_mismatch")

    @staticmethod
    def _verify_effective_limits(
        projection: Mapping[str, Any],
        *,
        max_attempts: int,
        max_outstanding_jobs: int | None,
    ) -> None:
        authority = projection.get("declared_authority")
        limits = authority.get("limits") if isinstance(authority, Mapping) else None
        if not isinstance(limits, Mapping):
            raise BrowserTaskError("durable effective limits are missing", code="authority_limits_missing")
        try:
            projected_attempts = int(limits.get("max_attempts"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise BrowserTaskError("durable effective attempts are invalid", code="authority_limits_invalid") from exc
        projected_outstanding = limits.get("max_outstanding_jobs")
        if projected_outstanding is not None:
            try:
                projected_outstanding = int(projected_outstanding)
            except (TypeError, ValueError, OverflowError) as exc:
                raise BrowserTaskError(
                    "durable effective outstanding limit is invalid",
                    code="authority_limits_invalid",
                ) from exc
        if projected_attempts != max_attempts or projected_outstanding != max_outstanding_jobs:
            raise BrowserTaskError(
                "durable effective limits changed after admission",
                code="authority_limits_stale",
            )

    @staticmethod
    def _safe_request_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for key in (
            "status",
            "method",
            "url",
            "url_digest",
            "redirect",
            "resource_type",
            "pinned_address_digest",
            "reason_code",
        ):
            if key in receipt:
                safe[key] = receipt[key]
        return safe

    def _terminal_replay_proof(
        self,
        projection: Mapping[str, Any],
        *,
        expected_job_id: str | None = None,
        workspace_root: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """Recover mutually bound artifact/readback/cleanup proof for replay.

        A terminal status is only replayable when the exact browser artifact is
        still present and both the artifact and readback identities are bound
        to this durable root.  A generic successful receipt from another job
        must never make a crashed browser execution look complete.
        """

        job_id = _text(expected_job_id) or _text(projection.get("job_id"))
        if not job_id or _text(projection.get("job_id")) != job_id:
            return None
        if projection.get("run_identity") != job_id:
            return None
        authority = projection.get("declared_authority") if isinstance(projection.get("declared_authority"), Mapping) else {}
        routine_parent_job_id = _text(authority.get("routine_parent_job_id"))
        if routine_parent_job_id:
            # Procedure v2 native Browser roots are durable children of the
            # exact running parent.  Standalone browser jobs retain the
            # historical root=self requirement.
            try:
                routine_parent_fence = int(authority.get("routine_parent_fencing_token") or 0)
                projected_parent_fence = int(projection.get("parent_fencing_token") or 0)
            except (TypeError, ValueError):
                return None
            if (
                _text(projection.get("root_run_identity")) != routine_parent_job_id
                or _text(projection.get("parent_run_identity")) != routine_parent_job_id
                or _text(projection.get("parent_job_id")) != routine_parent_job_id
                or projected_parent_fence != routine_parent_fence
                or routine_parent_fence <= 0
                or _text(authority.get("routine_step_id")) != "public_browser_check"
            ):
                return None
        elif projection.get("root_run_identity") != job_id:
            return None
        if projection.get("job_kind") != BROWSER_TASK_JOB_KIND:
            return None

        artifact_ref: str | None = None
        artifact_sha256: str | None = None
        artifacts = projection.get("artifacts")
        if isinstance(artifacts, list):
            for item in reversed(artifacts):
                if not isinstance(item, Mapping):
                    continue
                candidate_ref = _text(item.get("file_path"))
                candidate_digest = _text(item.get("content_sha256")).lower()
                if (
                    item.get("exists") is True
                    and item.get("artifact_type") == "browser_public_task_result"
                    and item.get("producer") == BROWSER_TASK_JOB_KIND
                    and _BROWSER_ARTIFACT_PATH.fullmatch(candidate_ref)
                    and _SHA256.fullmatch(candidate_digest)
                ):
                    expected_path = browser_artifact_path_for_job(job_id)
                    if candidate_ref != expected_path:
                        continue
                    expected_artifact_id = "art_" + hashlib.sha256(
                        "|".join(
                            (
                                BROWSER_TASK_JOB_KIND,
                                "browser_public_task_result",
                                job_id,
                                candidate_ref,
                                candidate_digest,
                            )
                        ).encode("utf-8")
                    ).hexdigest()[:24]
                    if _text(item.get("artifact_id")) != expected_artifact_id:
                        continue
                    if workspace_root is not None:
                        artifact_bytes = read_browser_artifact_bytes(
                            candidate_ref,
                            workspace_root=workspace_root,
                        )
                        if (
                            artifact_bytes is None
                            or hashlib.sha256(artifact_bytes).hexdigest() != candidate_digest
                        ):
                            continue
                    artifact_ref = candidate_ref
                    artifact_sha256 = candidate_digest
                    break

        readback_id: str | None = None
        verified_at: str | None = None
        effects = projection.get("effects")
        if isinstance(effects, list):
            for item in reversed(effects):
                if not isinstance(item, Mapping) or _text(item.get("receipt_kind")) != "readback":
                    continue
                if _text(item.get("effect_type")) != "browser_public_task_result":
                    continue
                if _text(item.get("status")) not in {"succeeded", "read_back", "reconciled"}:
                    continue
                candidate_id = _text(item.get("readback_id"))
                candidate_time = _text(item.get("verified_at"))
                candidate_path = _text(item.get("target_path"))
                candidate_digest = _text(item.get("content_sha256") or item.get("target_digest")).lower()
                details = item.get("details")
                expected_readback_id = "readback-" + _digest(
                    {"job_id": job_id, "path": artifact_ref, "digest": artifact_sha256}
                )[:32]
                verified_time_valid = False
                if candidate_time:
                    try:
                        verified_time_valid = datetime.fromisoformat(
                            candidate_time.replace("Z", "+00:00")
                        ).tzinfo is not None
                    except (TypeError, ValueError):
                        verified_time_valid = False
                if (
                    candidate_id == expected_readback_id
                    and verified_time_valid
                    and candidate_path
                    and candidate_digest
                    and _SHA256.fullmatch(candidate_digest)
                    and isinstance(details, Mapping)
                    and details.get("verified") is True
                    and _text(item.get("target_digest")).lower() == candidate_digest
                    and candidate_digest == artifact_sha256
                    and candidate_path == artifact_ref
                    and _text(item.get("job_id")) in {"", job_id}
                ):
                    readback_id = candidate_id
                    verified_at = candidate_time
                    break

        cleanup_status: str | None = None
        if isinstance(effects, list):
            for item in reversed(effects):
                if not isinstance(item, Mapping):
                    continue
                if (
                    _text(item.get("receipt_kind")) == "effect"
                    and _text(item.get("effect_type")) == "browser_context_cleanup"
                    and _text(item.get("status")) == "succeeded"
                ):
                    details = item.get("details")
                    if not isinstance(details, Mapping) or details.get("memory_status") != "no_learning":
                        continue
                    candidate_cleanup = details.get("cleanup_status")
                    if candidate_cleanup == "cleanup_verified":
                        valid_cleanup = details.get("context_not_started") is False
                    elif candidate_cleanup == "not_needed":
                        valid_cleanup = details.get("context_not_started") is True
                    else:
                        valid_cleanup = False
                    if valid_cleanup and _text(item.get("job_id")) in {"", job_id}:
                        cleanup_status = str(candidate_cleanup)
                        break

        if not (artifact_ref and artifact_sha256 and readback_id and verified_at and cleanup_status):
            return None
        return {
            "artifact_ref": artifact_ref,
            "artifact_sha256": artifact_sha256,
            "readback_id": readback_id,
            "verified_at": verified_at,
            "cleanup_status": cleanup_status,
            "memory_status": "no_learning",
        }

    def _receipt(self, **values: Any) -> dict[str, Any]:
        values.setdefault("job_id", f"browser-task:{values['task_id']}:{values['attempt_id']}")
        values.setdefault("action_count", 0)
        values.setdefault("request_count", 0)
        values.setdefault("action_receipts", [])
        values.setdefault("request_receipts", [])
        values.setdefault("checks", [])
        values.setdefault("cleanup_status", "not_needed")
        values.setdefault("memory_status", "no_learning")
        return BrowserTaskReceipt(
            capability_id=BROWSER_TASK_CAPABILITY_ID,
            durable_status=values.pop("durable_status", values.get("status", "blocked")),
            **values,
        ).model_dump(mode="json")

    async def _failure_receipt(
        self,
        *,
        task_id: str,
        attempt_id: str,
        job_id: str,
        error: BrowserTaskError,
    ) -> dict[str, Any]:
        durable_status = "unknown_external_effect" if error.dispatched else "blocked"
        observed = error.observed_request_receipts
        with suppress(Exception):
            current = await asyncio.wait_for(
                self.jobs.get_job(job_id),
                timeout=BROWSER_CLEANUP_TIMEOUT_SECONDS,
            )
            if not observed and isinstance(current, Mapping):
                observed = observed_browser_request_receipts(current)
            if isinstance(current, Mapping) and _text(current.get("status")) == "running":
                lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
                owner = _text(lease.get("owner"))
                fence = int(lease.get("fencing_token") or 0)
                if owner and fence > 0:
                    transition = await asyncio.wait_for(
                        self.jobs.transition_job(
                            job_id,
                            durable_status,
                            owner=owner,
                            fencing_token=fence,
                            expected_revision=int(current.get("revision") or 0),
                            reason=error.code,
                            result_summary=str(error),
                        ),
                        timeout=BROWSER_CLEANUP_TIMEOUT_SECONDS,
                    )
                    durable_status = _text(transition.get("status"), durable_status)
        status = "unknown_external_effect" if durable_status == "unknown_external_effect" else "blocked"
        return self._receipt(
            task_id=task_id,
            attempt_id=attempt_id,
            job_id=job_id,
            status=status,
            durable_status=durable_status,
            reason_code=error.code,
            cleanup_status=error.cleanup_status,
            request_count=len(observed),
            request_receipts=observed,
        )


def observed_browser_request_receipts(projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Positive bounded actual callbacks retained after rejected source use.

    A dispatch checkpoint proves admission only. Blocked transport callbacks
    and arbitrary effects cannot promote it into a received HTTP response.
    """
    job_id = _text(projection.get("job_id"))
    effects = projection.get("effects")
    if not job_id or not isinstance(effects, list):
        return []
    observed = {}
    for effect in effects[-100:]:
        if not isinstance(effect, Mapping) or effect.get("effect_type") != "browser_network_observation" or effect.get("status") != "succeeded":
            continue
        details = effect.get("details")
        if not isinstance(details, Mapping) or details.get("observation_only") is not True:
            continue
        count = details.get("request_count")
        receipt = details.get("request_receipt")
        if (type(count) is not int or not 1 <= count <= BROWSER_MAX_REQUESTS
            or effect.get("effect_id") != f"browser-network-observation:{job_id}:{count}"
            or not isinstance(receipt, Mapping) or type(receipt.get("status")) is not int
            or not 100 <= receipt["status"] <= 599):
            continue
        observed[count] = BrowserTaskRunner._safe_request_receipt(receipt)
    return [observed[count] for count in sorted(observed)]


@dataclass(slots=True)
class _ExecutionState:
    task_id: str
    attempt_id: str
    job_id: str
    owner_session_id: str
    owner_principal_id: str
    goal_id: str | None
    goal_revision: int | None
    board_task_revision: int
    admission_board_task_revision: int
    board_fencing_token: int
    input_artifact_id: str
    input_artifact_digest: str | None
    input_envelope_digest: str
    input_model_digest: str
    action_consent_digest: str
    action_count: int
    task_priority: int
    lease_owner: str
    fencing_token: int
    revision: int
    runtime_seconds: int = BROWSER_MAX_RUNTIME_SECONDS
    action_deadline_monotonic: float = 0.0
    execution_deadline_monotonic: float = 0.0
    context_revision: int = 1
    navigation_count: int = 0
    request_dispatches: int = 0
    request_receipt_events: int = 0
    request_count: int = 0
    current_action_index: int = -1
    network_dispatched: bool = False
    network_checkpointed: bool = False
    receipt_limit_hit: bool = False
    receipt_limit_code: str | None = None
    popup_blocked: bool = False
    download_blocked: bool = False
    cleanup_status: str = "cleanup_unknown"
    heartbeat_error: str | None = None
    action_receipts: list[dict[str, Any]] = None  # type: ignore[assignment]
    request_receipts: list[dict[str, Any]] = None  # type: ignore[assignment]
    checks: list[dict[str, Any]] = None  # type: ignore[assignment]
    extracts: list[dict[str, Any]] = None  # type: ignore[assignment]
    lock: asyncio.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.action_receipts = []
        self.request_receipts = []
        self.checks = []
        self.extracts = []
        self.lock = asyncio.Lock()


def _prefix_matches(url: str, prefix: str) -> bool:
    target = parse_public_https_url(url)
    approved = parse_public_https_url(prefix)
    if (target.hostname or "").lower().rstrip(".") != (approved.hostname or "").lower().rstrip("."):
        return False
    if (target.port or 443) != (approved.port or 443):
        return False
    if target.scheme.lower() != approved.scheme.lower():
        return False
    return target.query == approved.query and _path_prefix_match(target.path, approved.path)


__all__ = [
    "BROWSER_TASK_CAPABILITY_ID",
    "BROWSER_TASK_CAPABILITY_VERSION",
    "BrowserAction",
    "BrowserExpectedCheck",
    "BrowserPreflightReceipt",
    "BrowserInputError",
    "BrowserRuntimeControls",
    "BrowserTaskInput",
    "BrowserTaskReceipt",
    "BrowserTaskRunner",
    "BrowserTaskError",
    "BrowserUnknownExternalEffect",
    "BrowserVerificationError",
]


class ProfiledInteractionPage:
    """Current DOM actions in one registered, enforced offline public form.

    No caller selectors or scripts cross this boundary. Element handles and
    opaque references belong to one captured document, never a recovered job.
    """

    # Trusted introspection code; values are used only in private preview/digest.
    _STATE = """() => {const html=document.documentElement.outerHTML;
      const elements=Array.from(document.querySelectorAll('form input, form textarea, form select, form button'));
      if(new TextEncoder().encode(html).length>65536 || elements.length>64)
        return {blocked:'browser_document_bounds'};
      if(elements.some(e=>new TextEncoder().encode(e.value||'').length>2048 || (e.options&&e.options.length>64)))
        return {blocked:'browser_profile_field_bounds'};
      return {html,
      controls: elements
        .map(e => ({tag:e.tagName.toLowerCase(),type:e.type||'',field:e.name||'',
          value:e.value,checked:!!e.checked,disabled:!!e.disabled,
          name:(e.getAttribute('aria-label') || Array.from(e.labels||[])
            .map(l=>{const c=l.cloneNode(true);c.querySelectorAll('input,select,textarea,button')
              .forEach(n=>n.remove());return c.textContent}).join(' ') || e.textContent || '').trim(),
          options:e.tagName==='SELECT'?Array.from(e.options).map(o=>o.value):[]})),
      forms: Array.from(document.forms).map(f=>({method:f.method,action:f.action}))}}"""

    def __init__(self, *, authority, intent, result, request=None, browser_launcher=None, source_digest=None):
        self.authority, self.intent, self.result = authority, intent, result
        self.request, self.browser_launcher = request, browser_launcher
        self.source_digest = source_digest
        self.resources = _BrowserLaunchResources()
        self.transport = None
        self.page = None
        self.nodes = {}
        self.latest = None
        self.actions = 0
        self.lock = asyncio.Lock()

    async def start(self):
        from .interaction_contracts import DOCUMENT_URL, InteractionError
        from .pinned_transport import ProfiledPreparationTransport
        await self.authority()
        # Reuse v1's positively owned launch/teardown with identical isolation.
        launcher = BrowserTaskRunner(browser_launcher=self.browser_launcher)
        try:
            await launcher._launch_session(self.resources)
        except (ImportError, BrowserTaskError):
            if self.resources.context_not_started:
                raise InteractionError("browser_interaction_runtime_unavailable", status_code=503) from None
            raise
        self.page = await self.resources.context.new_page()

        async def reject_page(new_page):
            if new_page != self.page:
                await new_page.close()

        self.resources.context.on("page", reject_page)
        self.page.on("download", lambda download: asyncio.create_task(download.cancel()))
        self.transport = ProfiledPreparationTransport(request=self.request, source_digest=self.source_digest)
        await self.transport.install(self.resources.context, self.page,
            authority=self.authority,
            contact_intent=lambda: self.intent({"kind": "document", "phase": "document"}),
            contact_result=lambda response_digest: self.result({"kind": "document",
                "status": "completed", "response_digest": response_digest}))
        try:
            await self.page.goto(DOCUMENT_URL, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            raise InteractionError(self.transport.failure_reason or "browser_document_navigation_blocked") from None
        self.transport.preparation()
        if self.transport.denials:
            raise InteractionError("browser_profile_document_request_denied")
        return await self.snapshot()

    async def _state(self):
        from .interaction_contracts import DOCUMENT_URL, FIELDS, InteractionError, digest
        if self.page is None or self.page.url != DOCUMENT_URL:
            raise InteractionError("browser_origin_changed")
        state = await self.page.evaluate(self._STATE)
        if state.get("blocked"):
            raise InteractionError(state["blocked"])
        if len(str(state).encode()) > 65536 or len(state["controls"]) > 64:
            raise InteractionError("browser_document_bounds")
        if state["forms"] != [{"method": "post", "action": "https://httpbin.org/post"}]:
            raise InteractionError("browser_profile_form_changed")
        controls = state["controls"]
        if not controls or not {"custname", "comments", "size", "topping"}.issubset(
            {node["field"] for node in controls}):
            raise InteractionError("browser_profile_fields_changed")
        for node in controls:
            if node["tag"] == "button":
                continue
            if (node["field"] not in FIELDS or node["type"] in {"password", "file", "hidden"}
                or node["tag"] not in {"input", "textarea", "select"}
                or len(node["name"]) > 256):
                raise InteractionError("browser_profile_control_unsupported")
            if len(str(node["value"]).encode()) > 2048:
                raise InteractionError("browser_profile_field_bounds")
        return state, digest(state)

    async def snapshot(self):
        import uuid
        from .interaction_contracts import PageSnapshot, AccessibleNode, DOCUMENT_URL, InteractionError
        await self.authority()
        state, revision = await self._state()
        handles = await self.page.query_selector_all("form input, form textarea, form select, form button")
        if len(handles) != len(state["controls"]):
            raise InteractionError("browser_snapshot_drift")
        self.nodes = {}
        public = []
        # Reject equal accessibility identities rather than guessing a node.
        identities = [(n["tag"], n["type"], n["name"]) for n in state["controls"]]
        for handle, control, identity in zip(handles, state["controls"], identities):
            node_id = "node-" + uuid.uuid4().hex
            kind, tag = control["type"], control["tag"]
            actions = ([] if control["disabled"] or identities.count(identity) != 1 else
                ["check", "click"] if kind in {"checkbox", "radio"} else
                ["select"] if tag == "select" else
                ["fill"] if tag in {"input", "textarea"} and kind not in {"submit", "button", "reset"}
                else [])
            role = ("checkbox" if kind == "checkbox" else "radio" if kind == "radio" else
                "combobox" if tag == "select" else "button" if not actions else "textbox")
            self.nodes[node_id] = (handle, control, actions)
            public.append(AccessibleNode(node_id=node_id, role=role, name=control["name"], actions=actions))
        self.latest = PageSnapshot(url=DOCUMENT_URL, document_digest=revision,
            accessible_nodes=public, captured_at=datetime.now(timezone.utc))
        if len(self.latest.model_dump_json().encode()) > 32768:
            raise InteractionError("browser_snapshot_bounds")
        return self.latest.model_dump(mode="json")

    async def apply(self, action, private_value=None):
        from .interaction_contracts import InteractionError, MAX_ACTIONS
        async with self.lock:
            await self.authority()
            if self.actions >= MAX_ACTIONS:
                raise InteractionError("browser_action_limit")
            self.actions += 1
            # Persist intent even for rejected actions: reload explains the stop.
            intent = {"kind": action.kind, "locator_ref": action.locator_ref,
                "expected_page_revision": action.expected_page_revision, "phase": "preparation"}
            await self.intent(intent)
            try:
                _, revision = await self._state()
                if revision != action.expected_page_revision or self.latest is None:
                    raise InteractionError("browser_fresh_snapshot_required")
                target = self.nodes.get(action.locator_ref)
                if action.kind in {"click", "fill", "select", "check"}:
                    if target is None:
                        raise InteractionError("browser_fresh_snapshot_required")
                    handle, control, allowed = target
                    if action.kind not in allowed:
                        raise InteractionError("browser_exact_effect_authority_required")
                    await self.authority()
                    _, revision = await self._state()
                    if revision != action.expected_page_revision:
                        raise InteractionError("browser_fresh_snapshot_required")
                    if self.transport.phase != "preparation":
                        raise InteractionError("browser_preparation_not_offline")
                    if (action.kind in {"fill", "select"} and
                        (type(private_value) is not str or len(private_value.encode()) > 2048)):
                        raise InteractionError("browser_private_field_bounds")
                    if action.kind == "check" and type(private_value) is not bool:
                        raise InteractionError("browser_private_boolean_required")
                    if action.kind == "fill":
                        await handle.fill(private_value, timeout=2000)
                    elif action.kind == "select":
                        if private_value not in control["options"]:
                            raise InteractionError("browser_select_value_not_available")
                        await handle.select_option(value=private_value, timeout=2000)
                    elif action.kind == "check":
                        await handle.set_checked(private_value, timeout=2000)
                    elif action.kind == "click":
                        await handle.click(timeout=2000)
                elif action.kind == "navigate":
                    # Reopening is a new job/read consent, never an old contact replay.
                    raise InteractionError("browser_new_read_job_required")
                elif action.kind == "wait":
                    await self.page.wait_for_timeout(50)
                if self.transport.denials:
                    raise InteractionError("browser_preparation_egress_denied")
                state, _ = await self._state()
                payload = {"page": await self.snapshot()}
                if action.kind == "extract":
                    payload["preview"] = [{"field": n["field"], "value": n["value"],
                        "checked": n["checked"]} for n in state["controls"] if n["tag"] != "button"]
                    from .interaction_contracts import canonical
                    if len(canonical(payload["preview"])) > 16384:
                        raise InteractionError("browser_preview_bounds")
                await self.result({**intent, "status": "completed"})
                return payload
            except Exception as exc:
                code = exc.code if isinstance(exc, InteractionError) else "browser_action_failed"
                await self.result({**intent, "status": "blocked", "reason": code})
                raise InteractionError(code) from None

    async def stop(self):
        self.nodes.clear()
        try:
            clean = await asyncio.wait_for(self.resources.close(), timeout=10)
        except BaseException:
            clean = False
        return (clean and (self.resources.has_resources or self.resources.context_not_started)
                and (self.transport is None or self.transport.quiescent()))
