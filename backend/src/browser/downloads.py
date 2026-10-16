"""Quarantined public browser downloads (C5.3, #1014).

At most two operator-granted files per task, each at most 10 MiB, of a closed
content allowlist (PDF, DOCX, XLSX, CSV, plain text). Bytes arrive only through
this module's owned transport: the browser's requests are intercepted, every
URL and redirect hop must be in the grant, any non-GET or body-carrying request
(an upload) is aborted before contact, and the transport stops reading past the
size limit. Accepted files are sniffed (the claimed type must agree with the
bytes), written read-only (0400, never executable) into a private quarantine
with their exact source identity, read back and only then adopted. Nothing is
ever opened, rendered or executed by Seraph.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit
import zipfile

from pydantic import BaseModel, ConfigDict, Field, field_validator

CAPABILITY = "browser.download.v1"
PRODUCER = "browser.download.v1"
ARTIFACT_TYPE = "quarantined_download"
MAX_FILES = 2
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
PREVIEW_BYTES = 4096
QUARANTINE_PREFIX = "artifacts/browser-downloads/quarantine"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXTENSIONS = {"application/pdf": ".pdf", DOCX: ".docx", XLSX: ".xlsx", "text/csv": ".csv", "text/plain": ".txt"}
# Headers that do not contradict any concrete type; everything else must match.
GENERIC_CLAIMS = frozenset({"", "application/octet-stream", "binary/octet-stream"})
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,119}\Z")


class DownloadError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class BrowserFileRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime: Literal["application/pdf", DOCX, XLSX, "text/csv", "text/plain"]
    size: int = Field(ge=0, le=MAX_FILE_BYTES)
    permission: Literal["read_only_quarantined"] = "read_only_quarantined"


class DownloadGrant(BaseModel):
    """Exact operator-granted file URLs and the only allowed redirect targets."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    urls: list[str] = Field(min_length=1, max_length=MAX_FILES)
    redirect_urls: list[str] = Field(default_factory=list, max_length=MAX_REDIRECTS * MAX_FILES)

    @field_validator("urls", "redirect_urls")
    @classmethod
    def exact_urls(cls, values):
        for value in values:
            parts = urlsplit(value)
            if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password
                    or parts.fragment or len(value) > 2048):
                raise ValueError("grant URLs must be exact http(s) URLs without credentials or fragments")
        return values

    def allows(self, url: str) -> bool:
        return url in self.urls or url in self.redirect_urls


# ---------------------------------------------------------------- content policy

def _media_type(header: str | None) -> str:
    return (header or "").split(";", 1)[0].strip().lower()


def sniff_mime(data: bytes, *, suggested_filename: str = "") -> str:
    """Identify an allowed type from the bytes alone; anything else is refused."""
    if data[:4] in {b"\x7fELF", b"MZ\x90\x00"} or data[:2] in {b"MZ", b"#!"} or data[:4] == b"\xca\xfe\xba\xbe":
        raise DownloadError("download_executable_refused")
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist()[:2048])
        except (zipfile.BadZipFile, ValueError) as exc:
            raise DownloadError("download_content_unsupported") from exc
        if "[Content_Types].xml" in names and "word/document.xml" in names:
            return DOCX
        if "[Content_Types].xml" in names and "xl/workbook.xml" in names:
            return XLSX
        raise DownloadError("download_content_unsupported")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DownloadError("download_content_unsupported") from exc
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        raise DownloadError("download_content_unsupported")
    if text.lstrip()[:15].lower().startswith(("<!doctype", "<html", "<script", "<svg", "<?xml")):
        raise DownloadError("download_content_unsupported")
    return "text/csv" if suggested_filename.lower().endswith(".csv") else "text/plain"


def checked_mime(data: bytes, claimed: str | None, suggested_filename: str) -> str:
    sniffed = sniff_mime(data, suggested_filename=suggested_filename)
    media = _media_type(claimed)
    textual = {"text/plain", "text/csv"}
    if media in GENERIC_CLAIMS:
        return sniffed
    if media in textual and sniffed in textual:
        return media if media == "text/csv" or sniffed == "text/plain" else sniffed
    if media != sniffed:
        raise DownloadError("download_mime_mismatch")
    return sniffed


def safe_filename(suggested: str, mime: str) -> str:
    """A single plain basename whose extension matches the verified type."""
    name = str(suggested or "").strip()
    if (not name or "/" in name or "\\" in name or "\x00" in name or name in {".", ".."}
            or PurePosixPath(name).name != name or not _FILENAME.fullmatch(name)):
        raise DownloadError("download_filename_unsafe")
    if not name.lower().endswith(EXTENSIONS[mime]):
        raise DownloadError("download_extension_mismatch")
    return name


# ---------------------------------------------------------------- quarantine store

def _workspace_root() -> Path:
    from config.settings import settings
    from src.workspace import canonical_workspace_root
    return canonical_workspace_root(settings.workspace_dir)


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or (path.stat().st_mode & 0o077):
        raise DownloadError("download_quarantine_untrusted")


class DownloadQuarantine:
    """Read-only owned artifacts with exact source metadata; never executed."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else _workspace_root()

    def _job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", job_id):
            raise DownloadError("download_job_invalid")
        return self.root / QUARANTINE_PREFIX / hashlib.sha256(job_id.encode()).hexdigest()[:32]

    def admit(self, job_id: str, *, owner: Mapping[str, str], source_url: str, final_url: str,
              redirect_chain: Sequence[str], claimed_type: str | None, suggested_filename: str,
              data: bytes) -> BrowserFileRef:
        from src.artifacts.registry import build_artifact_record
        if len(data) > MAX_FILE_BYTES:
            raise DownloadError("download_oversized")
        mime = checked_mime(data, claimed_type, suggested_filename)
        name = safe_filename(suggested_filename, mime)
        directory = self._job_dir(job_id)
        _private_dir(directory)
        if len(self.list(job_id)) >= MAX_FILES:
            raise DownloadError("download_file_limit")
        digest = hashlib.sha256(data).hexdigest()
        relative = f"{directory.relative_to(self.root)}/{digest[:16]}-{name}"
        target = self.root / relative
        # Exclusive no-follow create, then read-only: never an executable file.
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        os.chmod(target, 0o400)
        readback = self._read(target, len(data))
        if hashlib.sha256(readback).hexdigest() != digest:
            target.unlink(missing_ok=True)
            raise DownloadError("download_readback_mismatch")
        record = build_artifact_record(file_path=relative, artifact_type=ARTIFACT_TYPE, producer=PRODUCER,
                                       run_id=job_id, session_id=owner.get("session_id"), content=readback,
                                       trust_boundary="quarantined_public_download",
                                       recovery_hint="Inspect only; Seraph never opens or executes downloads.")
        ref = BrowserFileRef(artifact_id=record["artifact_id"], digest=digest, mime=mime, size=len(data))
        metadata = {"schema_version": 1, "ref": ref.model_dump(), "job_id": job_id, "file_path": relative,
                    "filename": name, "owner_principal_id": owner.get("principal_id"),
                    "owner_session_id": owner.get("session_id"),
                    "source": {"requested_url": source_url, "final_url": final_url,
                               "redirect_chain": list(redirect_chain), "claimed_type": claimed_type or ""},
                    "artifact_record": record, "executed": False}
        sidecar = directory / f"{record['artifact_id']}.json"
        descriptor = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(metadata, handle, sort_keys=True)
        return ref

    @staticmethod
    def _read(path: Path, limit: int) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if not (info.st_mode & 0o170000 == 0o100000) or info.st_size > limit:
                raise DownloadError("download_quarantine_untrusted")
            chunks, remaining = [], info.st_size
            while remaining > 0:
                chunk = os.read(descriptor, min(1 << 20, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _metadata(self, artifact_id: str) -> tuple[Path, dict]:
        if not re.fullmatch(r"art_[0-9a-f]{24}", artifact_id):
            raise DownloadError("download_not_found")
        base = self.root / QUARANTINE_PREFIX
        for sidecar in base.glob(f"*/{artifact_id}.json") if base.is_dir() else ():
            return sidecar, json.loads(self._read(sidecar, 65536))
        raise DownloadError("download_not_found")

    def list(self, job_id: str) -> list[dict]:
        directory = self._job_dir(job_id)
        if not directory.is_dir():
            return []
        return [json.loads(self._read(path, 65536)) for path in sorted(directory.glob("art_*.json"))]

    def inspect(self, artifact_id: str, *, owner: Mapping[str, str]) -> dict:
        """Metadata plus a bounded inert preview; the file is never opened by an app."""
        _, metadata = self._metadata(artifact_id)
        if (metadata["owner_principal_id"], metadata["owner_session_id"]) != (owner.get("principal_id"),
                                                                               owner.get("session_id")):
            raise DownloadError("download_not_found")
        ref = BrowserFileRef(**metadata["ref"])
        data = self._read(self.root / metadata["file_path"], ref.size)
        if hashlib.sha256(data).hexdigest() != ref.digest:
            raise DownloadError("download_readback_mismatch")
        mode = (self.root / metadata["file_path"]).stat().st_mode & 0o777
        if ref.mime in {"text/plain", "text/csv"}:
            preview = {"kind": "text", "text": data[:PREVIEW_BYTES].decode("utf-8", errors="replace"),
                       "truncated": len(data) > PREVIEW_BYTES}
        elif ref.mime == "application/pdf":
            preview = {"kind": "pdf", "header": data[:8].decode("latin-1")}
        else:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                preview = {"kind": "ooxml_parts", "parts": sorted(archive.namelist())[:50]}
        return {"ref": ref.model_dump(), "filename": metadata["filename"], "source": metadata["source"],
                "file_mode": oct(mode), "executable": bool(mode & 0o111), "executed": False, "preview": preview}

    def revoke(self, artifact_id: str, *, owner: Mapping[str, str]) -> bool:
        sidecar, metadata = self._metadata(artifact_id)
        if (metadata["owner_principal_id"], metadata["owner_session_id"]) != (owner.get("principal_id"),
                                                                               owner.get("session_id")):
            raise DownloadError("download_not_found")
        target = self.root / metadata["file_path"]
        target.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        return not target.exists() and not sidecar.exists()


# ---------------------------------------------------------------- owned transport

class FetchedFile(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    requested_url: str
    final_url: str
    redirect_chain: list[str]
    content_type: str
    filename: str
    data: bytes


def _filename_from(headers: Mapping[str, str], url: str) -> str:
    disposition = headers.get("content-disposition", "")
    match = re.search(r'filename="?([^";]+)"?', disposition)
    return (match.group(1) if match else PurePosixPath(urlsplit(url).path).name).strip()


def _pinned_address(url: str, *, allow_local_fixture: bool, site_policy) -> str:
    """Resolve once, require every address public, and connect to that one."""
    parts = urlsplit(url)
    if parts.scheme != "https" and not allow_local_fixture:
        raise DownloadError("download_https_required")
    if not site_policy(url).allowed:
        raise DownloadError("download_site_policy_blocked")
    try:
        records = socket.getaddrinfo(parts.hostname or "", parts.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DownloadError("download_host_unresolved") from exc
    addresses = [record[4][0] for record in records]
    if not addresses:
        raise DownloadError("download_host_unresolved")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global and not (allow_local_fixture and ip.is_loopback):
            raise DownloadError("download_private_address_denied")
    return addresses[0]


class OwnedDownloadTransport:
    """Bounded pinned GET with in-grant redirects only; reads stop past the limit."""

    def __init__(self, grant: DownloadGrant, *, allow_local_fixture: bool = False, timeout: float = 20.0,
                 site_policy=None):
        from src.security.site_policy import evaluate_site_access
        self.grant, self.allow_local_fixture, self.timeout = grant, allow_local_fixture, timeout
        self.site_policy = site_policy or evaluate_site_access
        self.contacts: list[str] = []

    async def fetch(self, url: str) -> FetchedFile:
        import httpx
        try:
            return await self._fetch(url)
        except httpx.TimeoutException:
            raise DownloadError("download_network_timeout") from None
        except httpx.HTTPError:
            raise DownloadError("download_network_failed") from None

    async def _fetch(self, url: str) -> FetchedFile:
        import httpx
        chain: list[str] = []
        current = url
        async with httpx.AsyncClient(follow_redirects=False, timeout=self.timeout, trust_env=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                if not self.grant.allows(current):
                    raise DownloadError("download_redirect_outside_grant" if chain else "download_url_outside_grant")
                parts = urlsplit(current)
                address = _pinned_address(current, allow_local_fixture=self.allow_local_fixture,
                                          site_policy=self.site_policy)
                host = f"[{address}]" if ":" in address else address
                pinned = urlunsplit((parts.scheme, f"{host}:{parts.port}" if parts.port else host,
                                     parts.path or "/", parts.query, ""))
                extensions = {"sni_hostname": parts.hostname} if parts.scheme == "https" else {}
                self.contacts.append(current)
                async with client.stream("GET", pinned, extensions=extensions, headers={
                        "Host": parts.netloc, "Accept": "*/*", "Accept-Encoding": "identity"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        chain.append(current)
                        current = urljoin(current, response.headers.get("location", ""))
                        continue
                    if response.status_code != 200:
                        raise DownloadError("download_http_status")
                    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                        raise DownloadError("download_encoding_unsupported")
                    declared = response.headers.get("content-length")
                    if declared is not None and declared.isdigit() and int(declared) > MAX_FILE_BYTES:
                        raise DownloadError("download_oversized")
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        body.extend(chunk)
                        if len(body) > MAX_FILE_BYTES:
                            raise DownloadError("download_oversized")
                    return FetchedFile(requested_url=url, final_url=current, redirect_chain=chain,
                                       content_type=response.headers.get("content-type", ""),
                                       filename=_filename_from(response.headers, current), data=bytes(body))
        raise DownloadError("download_redirect_limit")


# ---------------------------------------------------------------- browser task

class DownloadOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[BrowserFileRef]
    blocked: list[dict[str, str]]
    upload_requests_rejected: int
    cleanup_verified: bool


async def download_public_files(grant: DownloadGrant, *, job_id: str, owner: Mapping[str, str],
                                quarantine: DownloadQuarantine | None = None, browser_launcher=None,
                                transport: OwnedDownloadTransport | None = None,
                                lane=None, timeout_seconds: float = 60.0) -> DownloadOutcome:
    """Navigate a JavaScript-free isolated browser to each granted file URL.

    Every browser request is intercepted: uploads and out-of-grant URLs are
    aborted before any contact; granted bytes come from the owned transport
    and are delivered to the browser as an attachment, so it never renders
    them. Each file is adopted only after quarantine readback.
    """
    from .task_runner import BrowserTaskRunner, _BrowserLaunchResources
    quarantine = quarantine or DownloadQuarantine()
    transport = transport or OwnedDownloadTransport(grant)
    fetched: dict[str, FetchedFile] = {}
    blocked: list[dict[str, str]] = []
    uploads = 0
    resources = _BrowserLaunchResources()
    events: list[Any] = []
    clean = True

    async def route(route_handle):
        nonlocal uploads
        request = route_handle.request
        if request.method != "GET" or request.post_data_buffer:
            uploads += 1
            blocked.append({"url": request.url, "code": "browser_upload_unsupported"})
            await route_handle.abort("blockedbyclient")
            return
        if request.url not in grant.urls:
            blocked.append({"url": request.url, "code": "download_url_outside_grant"})
            await route_handle.abort("blockedbyclient")
            return
        try:
            item = await transport.fetch(request.url)
        except DownloadError as exc:
            blocked.append({"url": request.url, "code": exc.code})
            await route_handle.abort("blockedbyclient")
            return
        fetched[request.url] = item
        # Attachment disposition: the browser saves (and here cancels), never renders.
        await route_handle.fulfill(status=200, body=item.data, headers={
            "content-type": "application/octet-stream",
            "content-disposition": 'attachment; filename="download.bin"'})

    launcher = BrowserTaskRunner(browser_launcher=browser_launcher)
    try:
        await launcher._launch_session(resources)
        context = resources.context
        await context.route("**/*", route)
        page = await context.new_page()
        page.on("download", lambda download: events.append(download))
        page.on("filechooser", lambda chooser: blocked.append({"url": page.url, "code": "browser_upload_unsupported"}))
        for url in grant.urls:
            try:
                await asyncio.wait_for(page.goto(url, wait_until="commit"), timeout_seconds)
            except Exception:
                pass  # A download navigation never commits a document.
        deadline = asyncio.get_running_loop().time() + 5
        while len(events) < len(fetched) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
    finally:
        # The same positively owned teardown as every browser task; an
        # unverified close keeps the shared browser lane quarantined.
        if resources.has_resources:
            clean = await resources.close()
        if not clean and lane is not None:
            lane.quarantine(job_id)
    seen = {getattr(event, "url", None) for event in events}
    files: list[BrowserFileRef] = []
    for url in grant.urls:
        item = fetched.get(url)
        if item is None:
            continue
        if url not in seen:
            blocked.append({"url": url, "code": "download_browser_event_missing"})
            continue
        try:
            files.append(quarantine.admit(job_id, owner=owner, source_url=item.requested_url,
                                          final_url=item.final_url, redirect_chain=item.redirect_chain,
                                          claimed_type=item.content_type, suggested_filename=item.filename,
                                          data=item.data))
        except DownloadError as exc:
            blocked.append({"url": url, "code": exc.code})
    return DownloadOutcome(files=files, blocked=blocked, upload_requests_rejected=uploads, cleanup_verified=clean)
