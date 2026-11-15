"""Quarantined public browser downloads (C5.3, #1014).

At most two operator-granted public HTTPS files per request, each at most
10 MiB, of a closed content allowlist (PDF, DOCX, XLSX, CSV, plain text). Bytes
arrive only through this module's owned transport: the browser's requests are
intercepted, every URL and redirect hop must be in the grant, any non-GET or
body-carrying request (an upload) is aborted before contact, DNS is resolved
once and pinned, and reads stop past the size limit. Accepted files are
identified from their bytes (the claimed type must agree), written read-only
(0400, never executable) into a private quarantine with their exact source
identity, read back and only then adopted. Nothing is ever opened, rendered or
executed by Seraph.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit
import zipfile

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

CAPABILITY = "browser.download.v1"
PRODUCER = "browser.download.v1"
ARTIFACT_TYPE = "quarantined_download"
MAX_FILES = 2
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_OOXML_ENTRIES = 2048
MAX_OOXML_UNCOMPRESSED = 64 * 1024 * 1024
PREVIEW_BYTES = 4096
DEFAULT_DEADLINE_SECONDS = 90.0
QUARANTINE_PARTS = ("artifacts", "browser-downloads", "quarantine")
QUARANTINE_PREFIX = "/".join(QUARANTINE_PARTS)
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXTENSIONS = {"application/pdf": ".pdf", DOCX: ".docx", XLSX: ".xlsx", "text/csv": ".csv", "text/plain": ".txt"}
# Headers that do not contradict any concrete type; everything else must match.
GENERIC_CLAIMS = frozenset({"", "application/octet-stream", "binary/octet-stream"})
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,119}\Z")
_ARTIFACT_ID = re.compile(r"art_[0-9a-f]{24}\Z")
_JOB_DIR = re.compile(r"[0-9a-f]{32}\Z")
# Active or embedded OOXML content: macros, OLE/ActiveX objects, embedded
# files and external workbook links are refused, never neutralised.
_OOXML_FORBIDDEN_PART = re.compile(
    r"(vbaproject|vbadata|activex|oleobject|/embeddings/|externallinks/|\.(bin|exe|dll|js|vbs|ps1|bat|cmd|com|scr|jar|msi)$)",
    re.IGNORECASE)
_EXTERNAL_RELATIONSHIP = re.compile(r"<Relationship\b[^>]*TargetMode\s*=\s*[\"']External[\"'][^>]*>", re.IGNORECASE)
_HYPERLINK_TYPE = re.compile(r"Type\s*=\s*[\"'][^\"']*/hyperlink[\"']", re.IGNORECASE)
# Text that a viewer could treat as active markup or a document language.
_ACTIVE_TEXT_START = ("<", "%!ps", "{\\rtf", "#!", "\x7felf", "mz")
_ACTIVE_TEXT_MARKERS = ("<script", "<iframe", "<html", "<svg", "<object", "<embed", "<meta", "<body",
                        "<?xml", "onload=", "onerror=", "javascript:")


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


def _fixture_loopback_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme == "http" and parts.hostname == "127.0.0.1" and not parts.username and not parts.fragment


class DownloadGrant(BaseModel):
    """Exact operator-granted public HTTPS file URLs and their allowed redirect targets.

    Validation uses the browser capability's public-URL parser (HTTPS, port
    443, no credentials, fragments, control characters or ambiguous paths).
    Offline tests may pass ``context={"allow_local_fixture": True}`` to admit
    a plain-HTTP 127.0.0.1 fixture origin; the API never sets it.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)
    urls: list[str] = Field(min_length=1, max_length=MAX_FILES)
    redirect_urls: list[str] = Field(default_factory=list, max_length=MAX_REDIRECTS * MAX_FILES)

    @field_validator("urls", "redirect_urls")
    @classmethod
    def exact_urls(cls, values, info: ValidationInfo):
        from .pinned_transport import PinnedTransportError, parse_public_https_url
        fixture = bool((info.context or {}).get("allow_local_fixture"))
        if len(set(values)) != len(values):
            raise ValueError("grant URLs must be distinct")
        for value in values:
            if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
                raise ValueError("grant URLs must not contain control characters")
            if fixture and _fixture_loopback_url(value):
                continue
            try:
                parse_public_https_url(value)
            except PinnedTransportError as exc:
                raise ValueError(f"grant URL rejected: {exc.code}") from None
        return values

    def allows(self, url: str) -> bool:
        return url in self.urls or url in self.redirect_urls

    def digest(self) -> str:
        return _canonical_digest(self.model_dump())


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------- content policy

def _media_type(header: str | None) -> str:
    return (header or "").split(";", 1)[0].strip().lower()


def _ooxml_kind(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_OOXML_ENTRIES or sum(entry.file_size for entry in entries) > MAX_OOXML_UNCOMPRESSED:
                raise DownloadError("download_content_unsupported")
            names = {entry.filename for entry in entries}
            if any(_OOXML_FORBIDDEN_PART.search(name) for name in names):
                raise DownloadError("download_active_content_refused")
            if "[Content_Types].xml" not in names:
                raise DownloadError("download_content_unsupported")
            types = archive.read("[Content_Types].xml")[:1 << 20].decode("utf-8", errors="replace").lower()
            if any(marker in types for marker in ("macroenabled", "vbaproject", "oleobject", "activex")):
                raise DownloadError("download_active_content_refused")
            for name in names:
                if name.endswith(".rels"):
                    with archive.open(name) as handle:
                        rels = handle.read(1 << 20).decode("utf-8", errors="replace")
                    for relationship in _EXTERNAL_RELATIONSHIP.findall(rels):
                        if not _HYPERLINK_TYPE.search(relationship):
                            raise DownloadError("download_active_content_refused")
    except (zipfile.BadZipFile, ValueError, KeyError, OSError, RuntimeError, NotImplementedError) as exc:
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError("download_content_unsupported") from exc
    if "word/document.xml" in names:
        return DOCX
    if "xl/workbook.xml" in names:
        return XLSX
    raise DownloadError("download_content_unsupported")


def sniff_mime(data: bytes, *, suggested_filename: str = "") -> str:
    """Identify an allowed type from the bytes alone; anything else is refused."""
    if data[:4] in {b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"} or data[:2] in {b"MZ", b"#!"}:
        raise DownloadError("download_executable_refused")
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        return _ooxml_kind(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DownloadError("download_content_unsupported") from exc
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        raise DownloadError("download_content_unsupported")
    head = text.lstrip("﻿ \t\r\n\f\v")[:64].lower()
    if head.startswith(("\x7felf", "mz", "#!")):
        raise DownloadError("download_executable_refused")
    lowered = text.lower()
    if head.startswith(_ACTIVE_TEXT_START) or any(marker in lowered for marker in _ACTIVE_TEXT_MARKERS):
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
    if (not name or "/" in name or "\\" in name or "\x00" in name or name in {".", ".."} or ".." in name
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


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _job_hash(job_id: str, owner: Mapping[str, str]) -> str:
    """The job directory is derived from the job and its owner, so ownership
    recorded in a sidecar or request record cannot be reassigned by editing it."""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", job_id):
        raise DownloadError("download_job_invalid")
    return _canonical_digest([job_id, owner.get("principal_id"), owner.get("session_id")])[:32]


def _recorded_owner(record: Mapping) -> dict:
    return {"principal_id": record.get("owner_principal_id"), "session_id": record.get("owner_session_id")}


def _read_fd(descriptor: int, limit: int) -> tuple[bytes, os.stat_result]:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise DownloadError("download_quarantine_untrusted")
    chunks, remaining = [], info.st_size
    while remaining > 0:
        chunk = os.read(descriptor, min(1 << 20, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks), info


def _read_at(dir_fd: int, name: str, limit: int) -> tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise DownloadError("download_not_found") from None
    except OSError:
        raise DownloadError("download_quarantine_untrusted") from None
    try:
        return _read_fd(descriptor, limit)
    finally:
        os.close(descriptor)


def _write_new(dir_fd: int, name: str, payload: bytes) -> None:
    descriptor = os.open(name, _CREATE_FLAGS, 0o600, dir_fd=dir_fd)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view):]
        os.fchmod(descriptor, 0o400)  # read-only, never executable
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DownloadQuarantine:
    """Read-only owned artifacts with exact source metadata; never executed.

    Every path is walked component by component from the workspace root with
    no-follow directory descriptors. Each private component must be owned by
    this user and closed to others. Sidecar metadata is bound by its own digest
    and by recomputing the artifact identity and the expected file name.
    """

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else _workspace_root()

    # -- directories
    def _walk(self, parts: Sequence[str], *, create: bool) -> int:
        try:
            descriptor = os.open(self.root, _DIR_FLAGS)
        except OSError:
            raise DownloadError("download_quarantine_untrusted") from None
        try:
            for index, name in enumerate(parts):
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(name, 0o700, dir_fd=descriptor)
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    raise DownloadError("download_not_found") from None
                except OSError:
                    # A symlink (ELOOP/ENOTDIR) or unreadable component.
                    raise DownloadError("download_quarantine_untrusted") from None
                os.close(descriptor)
                descriptor = child
                info = os.fstat(descriptor)
                # The shared workspace "artifacts" parent must be ours and not
                # world-writable; every quarantine component must be private.
                private = index > 0
                if info.st_uid != os.geteuid() or (info.st_mode & (0o077 if private else 0o002)):
                    raise DownloadError("download_quarantine_untrusted")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @contextlib.contextmanager
    def _job_dir(self, job_id: str, owner: Mapping[str, str], *, create: bool = False, lock: bool = False):
        descriptor = self._walk((*QUARANTINE_PARTS, _job_hash(job_id, owner)), create=create)
        try:
            if lock:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield descriptor
        finally:
            os.close(descriptor)

    # -- sidecars
    @staticmethod
    def _expected_name(ref: BrowserFileRef, filename: str) -> str:
        return f"{ref.digest[:16]}-{filename}"

    def _validated(self, raw: bytes, *, job_hash: str, artifact_id: str) -> dict:
        from src.artifacts.registry import artifact_id_for
        try:
            metadata = json.loads(raw)
            sealed = metadata.pop("sidecar_digest")
            ref = BrowserFileRef(**metadata["ref"])
            filename = safe_filename(metadata["filename"], ref.mime)
            job_id = metadata["job_id"]
        except (ValueError, KeyError, TypeError, DownloadError):
            raise DownloadError("download_quarantine_untrusted") from None
        expected_path = f"{QUARANTINE_PREFIX}/{job_hash}/{self._expected_name(ref, filename)}"
        if (sealed != _canonical_digest(metadata) or _job_hash(job_id, _recorded_owner(metadata)) != job_hash
                or metadata.get("file_path") != expected_path or ref.artifact_id != artifact_id
                or artifact_id_for(file_path=expected_path, artifact_type=ARTIFACT_TYPE, producer=PRODUCER,
                                   run_id=job_id, content_sha256=ref.digest) != artifact_id):
            raise DownloadError("download_quarantine_untrusted")
        return metadata

    def _sidecar_names(self, descriptor: int) -> list[str]:
        return sorted(name for name in os.listdir(descriptor)
                      if name.endswith(".json") and _ARTIFACT_ID.fullmatch(name[:-5]))

    @contextlib.contextmanager
    def _locate(self, artifact_id: str, *, lock: bool = False):
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
            raise DownloadError("download_not_found")
        base = self._walk(QUARANTINE_PARTS, create=False)
        try:
            for job_hash in sorted(name for name in os.listdir(base) if _JOB_DIR.fullmatch(name)):
                try:
                    descriptor = os.open(job_hash, _DIR_FLAGS, dir_fd=base)
                except OSError:
                    raise DownloadError("download_quarantine_untrusted") from None
                try:
                    info = os.fstat(descriptor)
                    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                        raise DownloadError("download_quarantine_untrusted")
                    if lock:
                        fcntl.flock(descriptor, fcntl.LOCK_EX)
                    try:
                        raw, _ = _read_at(descriptor, f"{artifact_id}.json", 65536)
                    except DownloadError as exc:
                        if exc.code == "download_not_found":
                            continue
                        raise
                    yield descriptor, self._validated(raw, job_hash=job_hash, artifact_id=artifact_id)
                    return
                finally:
                    os.close(descriptor)
        finally:
            os.close(base)
        raise DownloadError("download_not_found")

    @staticmethod
    def _owned(metadata: Mapping, owner: Mapping[str, str]) -> None:
        if (metadata["owner_principal_id"], metadata["owner_session_id"]) != (owner.get("principal_id"),
                                                                               owner.get("session_id")):
            raise DownloadError("download_not_found")

    # -- operations
    def admit(self, job_id: str, *, owner: Mapping[str, str], source_url: str, final_url: str,
              redirect_chain: Sequence[str], claimed_type: str | None, suggested_filename: str,
              data: bytes) -> BrowserFileRef:
        from src.artifacts.registry import build_artifact_record
        if len(data) > MAX_FILE_BYTES:
            raise DownloadError("download_oversized")
        mime = checked_mime(data, claimed_type, suggested_filename)
        name = safe_filename(suggested_filename, mime)
        digest = hashlib.sha256(data).hexdigest()
        job_hash = _job_hash(job_id, owner)
        relative = f"{QUARANTINE_PREFIX}/{job_hash}/{digest[:16]}-{name}"
        with self._job_dir(job_id, owner, create=True, lock=True) as descriptor:
            # Count and create under one exclusive job lock: never more than two.
            if len(self._sidecar_names(descriptor)) >= MAX_FILES:
                raise DownloadError("download_file_limit")
            created: list[str] = []
            try:
                try:
                    _write_new(descriptor, relative.rsplit("/", 1)[1], data)
                except FileExistsError:
                    raise DownloadError("download_duplicate_file") from None
                created.append(relative.rsplit("/", 1)[1])
                readback, _ = _read_at(descriptor, created[0], len(data))
                if hashlib.sha256(readback).hexdigest() != digest:
                    raise DownloadError("download_readback_mismatch")
                record = build_artifact_record(file_path=relative, artifact_type=ARTIFACT_TYPE, producer=PRODUCER,
                                               run_id=job_id, session_id=owner.get("session_id"), content=readback,
                                               trust_boundary="quarantined_public_download",
                                               recovery_hint="Inspect only; Seraph never opens or executes downloads.")
                ref = BrowserFileRef(artifact_id=record["artifact_id"], digest=digest, mime=mime, size=len(data))
                metadata = {"schema_version": 2, "ref": ref.model_dump(), "job_id": job_id, "file_path": relative,
                            "filename": name, "owner_principal_id": owner.get("principal_id"),
                            "owner_session_id": owner.get("session_id"),
                            "source": {"requested_url": source_url, "final_url": final_url,
                                       "redirect_chain": list(redirect_chain), "claimed_type": claimed_type or ""},
                            "artifact_record": record, "executed": False}
                metadata["sidecar_digest"] = _canonical_digest(metadata)
                sidecar = f"{record['artifact_id']}.json"
                _write_new(descriptor, sidecar, json.dumps(metadata, sort_keys=True).encode())
                created.append(sidecar)
                os.fsync(descriptor)
            except BaseException:
                # No orphan: remove whatever this admission created.
                for created_name in created:
                    with contextlib.suppress(OSError):
                        os.unlink(created_name, dir_fd=descriptor)
                raise
        return ref

    def list(self, job_id: str, *, owner: Mapping[str, str]) -> list[dict]:
        try:
            with self._job_dir(job_id, owner) as descriptor:
                job_hash = _job_hash(job_id, owner)
                return [self._validated(_read_at(descriptor, name, 65536)[0], job_hash=job_hash,
                                        artifact_id=name[:-5]) for name in self._sidecar_names(descriptor)]
        except DownloadError as exc:
            if exc.code == "download_not_found":
                return []
            raise

    def inspect(self, artifact_id: str, *, owner: Mapping[str, str]) -> dict:
        """Metadata plus a bounded inert preview; the file is never opened by an app."""
        with self._locate(artifact_id) as (descriptor, metadata):
            self._owned(metadata, owner)
            ref = BrowserFileRef(**metadata["ref"])
            data, info = _read_at(descriptor, metadata["file_path"].rsplit("/", 1)[1], ref.size)
        if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.digest:
            raise DownloadError("download_readback_mismatch")
        mode = stat.S_IMODE(info.st_mode)
        if ref.mime in {"text/plain", "text/csv"}:
            preview = {"kind": "text", "text": data[:PREVIEW_BYTES].decode("utf-8", errors="replace"),
                       "truncated": len(data) > PREVIEW_BYTES}
        elif ref.mime == "application/pdf":
            preview = {"kind": "pdf", "header": data[:8].decode("latin-1")}
        else:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                preview = {"kind": "ooxml_parts", "parts": sorted(archive.namelist())[:50]}
        # Preview bytes come from a remote site: display them, never follow them.
        preview["untrusted_content"] = True
        return {"ref": ref.model_dump(), "filename": metadata["filename"], "source": metadata["source"],
                "file_mode": oct(mode), "executable": bool(mode & 0o111), "executed": False, "preview": preview}

    def revoke(self, artifact_id: str, *, owner: Mapping[str, str]) -> bool:
        with self._locate(artifact_id, lock=True) as (descriptor, metadata):
            self._owned(metadata, owner)
            names = (metadata["file_path"].rsplit("/", 1)[1], f"{artifact_id}.json")
            for name in names:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
            remaining = set(os.listdir(descriptor))
        return not remaining.intersection(names)

    # -- request records (idempotent replay without re-contact)
    def file_state(self, artifact_id: str, *, owner: Mapping[str, str]) -> str:
        """present | revoked | untrusted, without reading the file contents."""
        try:
            with self._locate(artifact_id) as (descriptor, metadata):
                self._owned(metadata, owner)
                info = os.stat(metadata["file_path"].rsplit("/", 1)[1], dir_fd=descriptor, follow_symlinks=False)
                return "present" if stat.S_ISREG(info.st_mode) else "untrusted"
        except FileNotFoundError:
            return "revoked"
        except DownloadError as exc:
            return "revoked" if exc.code == "download_not_found" else "untrusted"

    def job_record(self, job_id: str, *, owner: Mapping[str, str]) -> dict | None:
        try:
            with self._job_dir(job_id, owner) as descriptor:
                raw, _ = _read_at(descriptor, "request.json", 65536)
        except DownloadError as exc:
            if exc.code == "download_not_found":
                return None
            raise
        try:
            record = json.loads(raw)
            sealed = record.pop("record_digest")
        except (ValueError, KeyError, TypeError):
            raise DownloadError("download_quarantine_untrusted") from None
        if (sealed != _canonical_digest(record) or record.get("job_id") != job_id
                or _job_hash(job_id, _recorded_owner(record)) != _job_hash(job_id, owner)):
            raise DownloadError("download_quarantine_untrusted")
        return record

    def begin_job(self, job_id: str, *, owner: Mapping[str, str], grant_digest: str) -> None:
        record = {"job_id": job_id, "owner_principal_id": owner.get("principal_id"),
                  "owner_session_id": owner.get("session_id"), "grant_digest": grant_digest, "state": "started"}
        with self._job_dir(job_id, owner, create=True, lock=True) as descriptor:
            try:
                _write_new(descriptor, "request.json",
                           json.dumps({**record, "record_digest": _canonical_digest(record)}).encode())
            except FileExistsError:
                raise DownloadError("download_request_in_progress") from None

    def finish_job(self, job_id: str, *, owner: Mapping[str, str], outcome: dict | None = None,
                   error: str | None = None) -> None:
        with self._job_dir(job_id, owner, lock=True) as descriptor:
            record = self.job_record(job_id, owner=owner)
            if record is None or record["state"] != "started":
                raise DownloadError("download_quarantine_untrusted")
            record.update(state="completed" if error is None else "failed", outcome=outcome, error=error)
            payload = json.dumps({**record, "record_digest": _canonical_digest(record)}).encode()
            with contextlib.suppress(FileNotFoundError):
                os.unlink("request.next", dir_fd=descriptor)
            _write_new(descriptor, "request.next", payload)
            os.rename("request.next", "request.json", src_dir_fd=descriptor, dst_dir_fd=descriptor)
            os.fsync(descriptor)


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


class OwnedDownloadTransport:
    """Bounded pinned GET with in-grant redirects only; reads stop past the limit.

    DNS runs on the browser transport's dedicated executor; every resolved
    address must be globally routable and the connection goes to that address
    with the original Host and SNI. One wall-clock deadline covers each file,
    including redirects.
    """

    def __init__(self, grant: DownloadGrant, *, allow_local_fixture: bool = False, timeout: float = 30.0,
                 site_policy=None, resolver=None):
        from src.security.site_policy import evaluate_site_access
        from .pinned_transport import _blocking_default_resolver
        self.grant, self.allow_local_fixture, self.timeout = grant, allow_local_fixture, timeout
        self.site_policy = site_policy or evaluate_site_access
        self.resolver = resolver or _blocking_default_resolver
        self.contacts: list[str] = []
        self._pending: set = set()

    async def _pinned_address(self, url: str) -> str:
        from .pinned_transport import _run_blocking
        parts = urlsplit(url)
        fixture = self.allow_local_fixture  # offline tests only: loopback fixture origins
        if parts.scheme != "https" and not fixture:
            raise DownloadError("download_https_required")
        if not self.site_policy(url).allowed:
            raise DownloadError("download_site_policy_blocked")
        try:
            addresses = await _run_blocking(self.resolver, parts.hostname or "", parts.port or 443,
                                            timeout_seconds=self.timeout, pending=self._pending)
        except (OSError, asyncio.TimeoutError):
            raise DownloadError("download_host_unresolved") from None
        if not addresses:
            raise DownloadError("download_host_unresolved")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(str(address).split("%", 1)[0])
            except ValueError:
                raise DownloadError("download_host_unresolved") from None
            if not ip.is_global and not (fixture and ip.is_loopback):
                raise DownloadError("download_private_address_denied")
        return str(addresses[0])

    async def fetch(self, url: str) -> FetchedFile:
        import httpx
        try:
            return await asyncio.wait_for(self._fetch(url), self.timeout)
        except (asyncio.TimeoutError, httpx.TimeoutException):
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
                address = await self._pinned_address(current)
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


async def _settle_browser(resources, lane, witness, job_id: str) -> bool:
    """Positive teardown, or keep the shared browser lane quarantined.

    A launch attempt whose handles never arrived, a close that fails, or a
    cancellation while closing are all unverified: the lane stays held.
    """
    if not resources.launch_attempted and not resources.has_resources:
        clean = True
    elif not resources.has_resources:
        clean = False
    else:
        close = asyncio.ensure_future(resources.close())
        try:
            clean = bool(await asyncio.shield(close))
        except BaseException:
            if lane is not None:
                lane.quarantine(job_id)
            raise
    if lane is not None:
        if clean and witness is not None:
            lane.confirm_positive_cleanup(witness)
        elif not clean:
            lane.quarantine(job_id)
    return clean


async def download_public_files(grant: DownloadGrant, *, job_id: str, owner: Mapping[str, str],
                                quarantine: DownloadQuarantine | None = None, browser_launcher=None,
                                transport: OwnedDownloadTransport | None = None, lane=None, witness=None,
                                deadline_seconds: float = DEFAULT_DEADLINE_SECONDS) -> DownloadOutcome:
    """Navigate a JavaScript-free isolated browser to each granted file URL.

    Every browser request is intercepted: uploads and out-of-grant URLs are
    aborted before any contact; granted bytes come from the owned transport
    and are delivered to the browser as an attachment, so it never renders
    them. Each file is adopted only after quarantine readback. One wall-clock
    deadline bounds the browser work; teardown always follows.
    """
    from .task_runner import BrowserTaskRunner, _BrowserLaunchResources
    quarantine = quarantine or DownloadQuarantine()
    transport = transport or OwnedDownloadTransport(grant)
    fetched: dict[str, FetchedFile] = {}
    blocked: list[dict[str, str]] = []
    uploads = 0
    resources = _BrowserLaunchResources()
    events: list[Any] = []

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

    async def browse():
        launcher = BrowserTaskRunner(browser_launcher=browser_launcher)
        await launcher._launch_session(resources)
        context = resources.context
        await context.route("**/*", route)
        page = await context.new_page()
        page.on("download", lambda download: events.append(download))
        page.on("filechooser", lambda chooser: blocked.append({"url": page.url, "code": "browser_upload_unsupported"}))
        for url in grant.urls:
            with contextlib.suppress(Exception):
                await page.goto(url, wait_until="commit")  # a download navigation never commits
        while len(events) < len(fetched):
            await asyncio.sleep(0.05)

    clean = False
    try:
        try:
            await asyncio.wait_for(browse(), deadline_seconds)
        except asyncio.TimeoutError:
            raise DownloadError("download_deadline_exceeded") from None
    finally:
        clean = await _settle_browser(resources, lane, witness, job_id)
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


async def run_download_request(grant: DownloadGrant, *, job_id: str, owner: Mapping[str, str],
                               workspace_root=None, quarantine: DownloadQuarantine | None = None,
                               **options) -> dict:
    """One request key, one outcome: a replay returns the recorded result and never re-contacts.

    The request occupies the one shared browser lane with the same staged
    cleanup witness as every browser task; an unverified teardown leaves the
    lane quarantined (``browser_cleanup_required``).
    """
    from config.settings import settings
    from .task_lane import BrowserTaskLaneError, try_acquire_browser_task_lane
    from .task_runner import BrowserTaskError
    quarantine = quarantine or DownloadQuarantine()
    grant_digest = grant.digest()
    existing = quarantine.job_record(job_id, owner=owner)
    if existing is not None:
        if existing["grant_digest"] != grant_digest:
            raise DownloadError("download_request_key_body_changed")
        if existing["state"] == "completed":
            outcome = existing["outcome"]
            # Report what still exists: a revoked file is not silently re-offered.
            states = {ref["artifact_id"]: quarantine.file_state(ref["artifact_id"], owner=owner)
                      for ref in outcome["files"]}
            return {"job_id": job_id, "replayed": True, **outcome, "file_states": states}
        if existing["state"] == "failed":
            raise DownloadError(existing["error"])
        raise DownloadError("download_request_outcome_unknown")
    lane = try_acquire_browser_task_lane(workspace_root or settings.workspace_dir)
    if lane is None:
        raise DownloadError("browser_task_lane_busy")
    try:
        try:
            witness = lane.stage_prelaunch(job_id)
        except BrowserTaskLaneError:
            raise DownloadError("browser_native_boot_proof_unavailable") from None
        try:
            quarantine.begin_job(job_id, owner=owner, grant_digest=grant_digest)
        except BaseException:
            # This owner has not entered any launcher boundary.
            lane.confirm_positive_cleanup(witness)
            raise
        try:
            lane.require_positive_cleanup(job_id)
        except BrowserTaskLaneError:
            # The durable witness could not be written; no launcher was entered.
            quarantine.finish_job(job_id, owner=owner, error="browser_cleanup_witness_unavailable")
            with contextlib.suppress(BrowserTaskLaneError):
                lane.confirm_positive_cleanup(witness)
            raise DownloadError("browser_cleanup_witness_unavailable") from None
        try:
            outcome = await download_public_files(grant, job_id=job_id, owner=owner, quarantine=quarantine,
                                                  lane=lane, witness=witness, **options)
        except DownloadError as exc:
            quarantine.finish_job(job_id, owner=owner, error=exc.code)
            raise
        except (ImportError, BrowserTaskError):
            quarantine.finish_job(job_id, owner=owner, error="browser_interaction_runtime_unavailable")
            raise DownloadError("browser_interaction_runtime_unavailable") from None
        result = outcome.model_dump(mode="json")
        quarantine.finish_job(job_id, owner=owner, outcome=result)
        return {"job_id": job_id, "replayed": False, **result,
                "file_states": {ref["artifact_id"]: "present" for ref in result["files"]}}
    finally:
        lane.release()
