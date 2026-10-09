"""Owner-selected private extraction on the current Python lifecycle owners."""
from __future__ import annotations

import asyncio
import anyio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct
import sys
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.work_board.repository import BoardError
from src.work_board import document_pairs as sources
from src.work_board.document_read_parser import canonical, SOURCE_LIMIT, OUTPUT_LIMIT
from src.work_board.input_artifacts import _begin_immediate, _metadata_digest
from src.work_board.input_artifacts import _open_input_artifact_parent, _private_input_file_metadata
from src.work_board.pipelines import root_binding
from src.auth.service import AuthFailure

CAPABILITY = "document.read.v1"


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class DocumentSelection(ClosedModel):
    pages: list[int] = Field(default_factory=list, max_length=100)
    sheets: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def finite_selection(self):
        if any(not 1 <= page <= 100 for page in self.pages) or len(set(self.pages)) != len(self.pages):
            raise ValueError("pages require unique physical ordinals 1..100")
        if any(not 0 < len(sheet) <= 128 for sheet in self.sheets) or len(set(self.sheets)) != len(self.sheets):
            raise ValueError("sheets require unique bounded literal names")
        return self


class DocumentLimits(ClosedModel):
    max_pages: int = Field(default=100, ge=1, le=100)
    max_sheets: int = Field(default=16, ge=1, le=16)
    max_cells: int = Field(default=100000, ge=1, le=100000)


class DocumentReadInput(ClosedModel):
    artifact_ref: str = Field(pattern=r"^document-source:[0-9a-f-]{36}$")
    format: Literal["pdf", "docx", "xlsx", "csv"]
    selection: DocumentSelection = Field(default_factory=DocumentSelection)
    page_sheet_limits: DocumentLimits = Field(default_factory=DocumentLimits)

    @model_validator(mode="after")
    def compatible_selection(self):
        if self.selection.pages and self.format != "pdf" or self.selection.sheets and self.format != "xlsx":
            raise ValueError("page and sheet selections must match the selected format")
        return self


class DocumentCell(ClosedModel):
    source_ref: str = Field(max_length=512)
    text: str
    formula: str | None
    cached_value: str | None


class DocumentSection(ClosedModel):
    source_ref: str = Field(max_length=512)
    text: str
    table_cells: list[DocumentCell]


class DocumentEvidence(ClosedModel):
    sections: list[DocumentSection]
    warnings: list[str]
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    no_learning: Literal[True]


class SourceDescriptor(ClosedModel):
    size_bytes: int = Field(ge=1, le=SOURCE_LIMIT)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentSourceReserve(ClosedModel):
    format: Literal["pdf", "docx", "xlsx", "csv"]
    source: SourceDescriptor
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    no_learning: Literal[True]

    @model_validator(mode="after")
    def docx_limit(self):
        if self.format == "docx" and self.source.size_bytes > 10*1024*1024:
            raise ValueError("DOCX source exceeds 10 MiB")
        return self


def projection(row):
    value = sources.metadata(row)
    return {"artifact_id": row.artifact_id, "artifact_ref": value["input"]["artifact_ref"],
        "revision": row.revision, "state": value["phase"], "format": value["input"]["format"],
        "source_digest": value["input"]["source"]["sha256"], "goal_id": row.goal_id,
        "goal_revision": row.goal_revision, "reason_code": value.get("reason"),
        "ingest_deadline": value["ingest_deadline"], "no_learning": True,
        "cleanup": "unknown_writer_retained" if value.get("live_writer") else "quiescent",
        "writer_kind": ("parser" if value["live_writer"].get("slot") == "parser" else "upload") if value.get("live_writer") else None,
        "provider_contacts": 0}


def read_witness(row, value):
    binding = value["parser_binding"]
    parent, _leaf = _open_input_artifact_parent(sources.source_path(row, value, "source"), create=False)
    descriptor = -1
    try:
        descriptor = os.open(binding["nonce"]+".witness.json", os.O_RDONLY|os.O_NOFOLLOW, dir_fd=parent)
        metadata = os.fstat(descriptor)
        if not _private_input_file_metadata(metadata) or not 0 < metadata.st_size <= 4096:
            raise ValueError("witness metadata changed")
        raw = os.read(descriptor, 4097)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)
    witness = json.loads(raw)
    fields = {"job_id", "input_digest", "generation", "nonce", "supervisor_pid", "parser_pid", "parser_exit", "wait_reaped", "reason"}
    if (set(witness) != fields or witness["wait_reaped"] is not True
        or type(witness["parser_exit"]) is not int
        or any(type(witness[key]) is not int or witness[key] <= 0 for key in ("supervisor_pid", "parser_pid"))
        or any(witness.get(key) != expected for key, expected in binding.items())):
        raise ValueError("original witness binding changed")
    return sources.sha256(raw)


async def reconcile_reader(db, owner, identifier, revision):
    row, value = await sources.owned(db, owner, identifier, revision=revision, capability=CAPABILITY)
    if value["root"] != dict(root_binding()):
        raise BoardError("document_pair_root_changed", "Reconcile only the original workspace", status_code=409)
    if not value.get("live_writer") or value["live_writer"].get("slot") != "parser":
        raise BoardError("document_original_reader_required", "There is no unknown original parser to reconcile", status_code=409)
    try:
        witness_digest = read_witness(row, value)
    except (OSError, ValueError, KeyError):
        raise BoardError("document_parser_cleanup_unknown", "The exact positive wait witness is unavailable; retain capacity", status_code=409) from None
    await _begin_immediate(db)
    fresh, current = await sources.owned(db, owner, identifier, revision=revision, capability=CAPABILITY)
    if current != value:
        raise BoardError("document_reader_fence_changed", "The original reader changed", status_code=409)
    current["live_writer"] = None
    current["witness_digest"] = witness_digest
    current["reason"] = "document_original_reader_reaped_output_unavailable"
    fresh.document_metadata_json = canonical(current).decode(); fresh.revision += 1
    fresh.metadata_digest = _metadata_digest(fresh)
    await db.commit()
    return projection(fresh)


async def current_source_root(db, owner, operator):
    """Native consumers also prove the live canonical Root, never a label."""
    from src.db.models import OperatorSession
    from src.auth.service import _aware
    root = await db.get(OperatorSession, owner.session_id, populate_existing=True)
    stamp = datetime.now(timezone.utc)
    if (root is None or root.principal_id != owner.principal_id or root.is_bearer_tombstone
        or root.revoked_at is not None or stamp >= _aware(root.idle_expires_at)
        or stamp >= _aware(root.absolute_expires_at)):
        raise BoardError("document_current_root_required", "Sign in and select a document under the current Root", status_code=409)
    if operator is not None:
        from src.auth.ownership import _current_root
        await _current_root(db, operator)
    return root


def remove_unadopted_output(path, receipt):
    """Remove only the exact unpublished ciphertext, with positive absence."""
    parent, leaf = _open_input_artifact_parent(path, create=False)
    fd = -1
    try:
        fd = os.open(leaf, os.O_RDONLY|os.O_NOFOLLOW, dir_fd=parent)
        facts = os.fstat(fd)
        if not _private_input_file_metadata(facts) or facts.st_size != receipt["cipher_size"] or facts.st_size > (OUTPUT_LIMIT+1024)*2:
            raise OSError("unadopted output metadata changed")
        raw = bytearray()
        while len(raw) < facts.st_size:
            chunk = os.read(fd, min(65536, facts.st_size-len(raw)))
            if not chunk: raise OSError("unadopted output truncated")
            raw.extend(chunk)
        if sources.sha256(bytes(raw)) != receipt["cipher_sha256"]:
            raise OSError("unadopted output digest changed")
        current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if any(getattr(current, key) != getattr(facts, key) for key in ("st_dev", "st_ino", "st_uid", "st_mode", "st_size", "st_nlink")):
            raise OSError("unadopted output name changed")
        os.unlink(leaf, dir_fd=parent); os.fsync(parent)
        try: os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError: return
        raise OSError("unadopted output absence unproven")
    finally:
        if fd >= 0: os.close(fd)
        os.close(parent)


async def shield_positive_cleanup(operation):
    """Await real cleanup even through direct asyncio and AnyIO cancellation."""
    async def run():
        with anyio.CancelScope(shield=True):
            return await operation
    cleanup = asyncio.create_task(run())
    cancelled = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancelled = exc
    value = cleanup.result()
    if cancelled is not None: raise cancelled
    return value


class DocumentService:
    """One CPU extraction at a time; no inference lane, queue or global activation."""
    def __init__(self):
        self._started = False
        self._processes = set()
        self._supervisors = set()
        self._process_deadlines = {}
        self._capacity = asyncio.Lock()
        self._upload_profile = None

    async def start(self):
        if self._upload_profile is not None: self._upload_profile["active"] = False
        self._upload_profile = None
        try:
            self._upload_profile = await sources.probe_upload_profile()
        except (OSError, ValueError, TimeoutError):
            self._upload_profile = None
        self._started = True

    async def stop(self):
        self._started = False
        if self._upload_profile is not None: self._upload_profile["active"] = False
        self._upload_profile = None
        for process in tuple(self._processes):
            if process.returncode is None:
                if process in self._supervisors:
                    process.stdin.close()
                else:
                    process.kill()
            await asyncio.wait_for(process.wait(), max(.001, self._process_deadlines[process]-time.monotonic()))

    async def parse(self, raw: bytes, request: DocumentReadInput, *, timeout=35, witness_directory=None, binding=None, on_ready=None, absolute_deadline=None):
        if not self._started:
            raise BoardError("document_service_inactive", "Start the managed document service", status_code=503)
        if self._capacity.locked():
            raise BoardError("document_parser_capacity_held", "Another local parser holds capacity; retry after cleanup", status_code=409)
        async with self._capacity:
            payload = canonical(request.model_dump())
            if not 0 < len(raw) <= SOURCE_LIMIT:
                raise BoardError("document_source_size_exceeded", "Select a bounded document", status_code=422)
            arguments = [sys.executable, "-I", str(Path(__file__).with_name("document_read_child.py"))]
            deadline = time.monotonic()+min(40, max(0, (absolute_deadline-time.time()) if absolute_deadline else 40))
            if witness_directory is not None:
                arguments = [sys.executable, "-I", str(Path(__file__).with_name("document_compare_supervisor.py")),
                    str(witness_directory), canonical(binding).decode(), str(min(time.time()+40, absolute_deadline or time.time()+40)), "general-read"]
            try:
                process = await asyncio.create_subprocess_exec(*arguments, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    env={"OPENBLAS_NUM_THREADS": "1"}, close_fds=True,
                    pass_fds=(witness_directory,) if witness_directory is not None else ())
            except OSError:
                raise BoardError("document_parser_not_started", "No parser was launched; restore the local process profile", status_code=503) from None
            self._processes.add(process)
            self._process_deadlines[process] = deadline
            if witness_directory is not None:
                self._supervisors.add(process)
            reason = None
            output = bytearray()
            async def communicate():
                ready = await process.stdout.readline()
                if len(ready) > 4096:
                    raise ValueError("readiness exceeds allowance")
                handshake = json.loads(ready)
                valid = handshake.get("state") == "ready" and handshake.get("network_denied") is True
                if binding is not None:
                    valid = valid and handshake.get("nonce") == binding["nonce"] and handshake.get("binding") == binding
                    valid = valid and handshake.get("supervisor_pid") == process.pid and type(handshake.get("parser_pid")) is int
                if not valid:
                    output.extend(ready)
                    process.stdin.close()
                    await process.wait()
                    return
                if on_ready is not None:
                    await on_ready(handshake)
                async def feed():
                    process.stdin.write(struct.pack("!II", len(payload), len(raw)))
                    process.stdin.write(payload); process.stdin.write(raw)
                    await process.stdin.drain(); process.stdin.close()
                async def receive():
                    while chunk := await process.stdout.read(65536):
                        output.extend(chunk)
                        if len(output) > OUTPUT_LIMIT + 4096:
                            raise ValueError("output exceeds pipe allowance")
                await asyncio.gather(feed(), receive())
                await process.wait()
            try:
                await asyncio.wait_for(communicate(), timeout=max(.001, min(timeout, 35, deadline-time.monotonic()-5)))
            except TimeoutError:
                reason = "document_parser_timeout"
            except (OSError, ValueError, BrokenPipeError):
                reason = "document_parser_crashed_or_output_exceeded"
            finally:
                if process.returncode is None:
                    if witness_directory is None:
                        process.kill()
                    elif process.stdin is not None:
                        # Supervisor independently kills/waits and writes the
                        # positive witness; never kill that witness owner.
                        process.stdin.close()
                # Actual positive wait, including request cancellation, precedes reuse.
                with anyio.CancelScope(shield=True):
                    await asyncio.shield(asyncio.wait_for(process.wait(), max(.001, deadline-time.monotonic())))
                self._processes.discard(process)
                self._supervisors.discard(process)
                self._process_deadlines.pop(process, None)
            if reason or process.returncode != 0:
                return {"status": "blocked", "reason": reason or "document_parser_resource_exit",
                    "cleanup": "wait_reaped", "no_learning": True, "provider_contacts": 0}
            try:
                result = json.loads(output)
                if result.get("status") == "succeeded":
                    result["evidence"] = DocumentEvidence.model_validate(result["evidence"]).model_dump()
                    if result["evidence"]["source_digest"] != sources.sha256(raw):
                        raise ValueError()
                elif result.get("status") != "blocked":
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                result = {"status": "blocked", "reason": "document_parser_output_invalid", "no_learning": True}
            return {**result, "cleanup": "wait_reaped", "provider_contacts": 0}

    async def read(self, db, owner, request: DocumentReadInput, *, operator=None):
        identifier = request.artifact_ref.split(":", 1)[1]
        root = dict(root_binding())
        row, value = await sources.owned(db, owner, identifier, capability=CAPABILITY)
        await sources.authority(db, owner, row, value, root)
        authenticated_root = await current_source_root(db, owner, operator)
        if value["phase"] != "sealed" or row.metadata_digest != _metadata_digest(row) or value["input"]["format"] != request.format:
            raise BoardError("document_source_not_ready", "Seal the exact selected source before reading", status_code=409)
        request_digest = sources.sha256(canonical(request.model_dump()))
        if value.get("read_request_digest") and value["read_request_digest"] != request_digest:
            raise BoardError("document_selection_changed", "Reserve a fresh source for another bounded selection", status_code=409)
        if value.get("reason") == "document_output_cleanup_required":
            raise BoardError("document_output_cleanup_required", "Delete the positively closed source generation before reserving a fresh document", status_code=409)
        if value.get("evidence"):
            raw = sources.read_private(sources.source_path(row, value, "evidence"), value["evidence"], maximum=OUTPUT_LIMIT)
            evidence = DocumentEvidence.model_validate_json(raw).model_dump()
            if evidence["source_digest"] != value["input"]["source"]["sha256"]:
                raise BoardError("document_evidence_source_changed", "The evidence digest no longer matches its selected source", status_code=409)
            return {"status": "succeeded", "evidence": evidence, "cleanup": "wait_reaped", "provider_contacts": 0}
        raw = sources.read_private(sources.source_path(row, value, "source"), value["sources"]["source"], maximum=SOURCE_LIMIT)
        if sources.sha256(raw) != value["input"]["source"]["sha256"]:
            raise BoardError("document_source_changed", "Restore the exact immutable source", status_code=409)
        token = uuid.uuid4().hex
        from src.work_board.document_capacity import stage_capacity, assert_capacity
        capacity_snapshot = await stage_capacity(db)
        await _begin_immediate(db)
        fresh, current = await sources.owned(db, owner, identifier, revision=row.revision, capability=CAPABILITY)
        await sources.authority(db, owner, fresh, current, root)
        if current.get("live_writer"):
            raise BoardError("document_parser_cleanup_unknown", "Reconcile the original reader before reuse", status_code=409)
        await assert_capacity(db, snapshot=capacity_snapshot)
        current["live_writer"] = {"token": token, "slot": "parser"}
        attempts = current.get("parser_attempts", 0)
        if attempts >= 2:
            raise BoardError("document_parser_attempt_limit", "Delete the retained source and select a fresh document", status_code=409)
        current["parser_attempts"] = attempts+1
        from datetime import timedelta
        from src.work_board.pipelines import now, utc
        if not current.get("execution_deadline"):
            deadlines = [utc(row.expires_at), now()+timedelta(seconds=70)]
            deadlines.extend([utc(authenticated_root.idle_expires_at), utc(authenticated_root.absolute_expires_at)])
            current["execution_deadline"] = min(deadlines).isoformat()
        remaining = (datetime.fromisoformat(current["execution_deadline"])-now()).total_seconds()
        if remaining < 40:
            raise BoardError("document_original_execution_window_expired", "Delete the closed source and select a fresh document", status_code=409)
        binding = {"job_id": identifier, "input_digest": request_digest, "generation": current["generation"], "nonce": token}
        current["parser_binding"] = binding
        current["read_request_digest"] = request_digest
        # All fallible filesystem preparation precedes durable parser ownership.
        directory, _leaf = _open_input_artifact_parent(sources.source_path(fresh, current, "source"), create=False)
        try:
            fresh.document_metadata_json = canonical(current).decode(); fresh.revision += 1
            fresh.metadata_digest = _metadata_digest(fresh)
            await db.commit()
        except BaseException:
            os.close(directory)
            raise
        result = None
        receipt = None
        ready_binding = binding
        adoption_error = None
        request_task = asyncio.current_task()
        final = latest = None
        async def ready(handshake):
            nonlocal ready_binding
            staged_root = dict(root_binding())
            await _begin_immediate(db)
            ready_row, ready_value = await sources.owned(db, owner, identifier, capability=CAPABILITY)
            if ready_value.get("live_writer") != {"token": token, "slot": "parser"}:
                raise BoardError("document_reader_fence_changed", "Original reader binding changed", status_code=409)
            await sources.authority(db, owner, ready_row, ready_value, staged_root)
            await current_source_root(db, owner, operator)
            if (datetime.fromisoformat(ready_value["execution_deadline"])-now()).total_seconds() < 35:
                raise BoardError("document_original_execution_window_expired", "The original parser window cannot cover execution and cleanup", status_code=409)
            ready_binding = {**binding, "supervisor_pid": handshake["supervisor_pid"], "parser_pid": handshake["parser_pid"]}
            ready_value["parser_binding"] = ready_binding
            ready_row.document_metadata_json = canonical(ready_value).decode(); ready_row.revision += 1
            ready_row.metadata_digest = _metadata_digest(ready_row)
            await db.commit()
        try:
            result = await self.parse(raw, request, witness_directory=directory, binding=binding, on_ready=ready,
                absolute_deadline=datetime.fromisoformat(current["execution_deadline"]).timestamp())
            if result["status"] == "succeeded":
                evidence_raw = canonical(result["evidence"])
                candidate = sources.publish_private(sources.source_path(fresh, current, "evidence"), evidence_raw)
                if sources.read_private(sources.source_path(fresh, current, "evidence"), candidate, maximum=OUTPUT_LIMIT) != evidence_raw:
                    raise ValueError("private evidence readback changed")
                receipt = candidate
        except (ValueError, OSError):
            result = {"status": "blocked", "reason": "document_output_cleanup_required",
                "cleanup": "wait_reaped", "no_learning": True, "provider_contacts": 0}
        except BoardError as exc:
            if exc.code not in {"document_parser_not_started", "document_service_inactive", "document_parser_capacity_held", "document_source_size_exceeded"}:
                raise
            result = {"status": "blocked", "reason": exc.code,
                "cleanup": "not_launched", "no_learning": True, "provider_contacts": 0}
        finally:
            os.close(directory)
            adoption_workspace_root = dict(root_binding())
            # Cancellation has already positively waited in parse(). A crash of
            # this owner before this commit leaves unknown capacity visibly held.
            async def settle():
                nonlocal final, latest, adoption_error
                async with asyncio.timeout(10):
                    await _begin_immediate(db)
                    final, latest = await sources.owned(db, owner, identifier, capability=CAPABILITY)
                    if (latest.get("live_writer") != {"token": token, "slot": "parser"}
                        or latest.get("generation") != binding["generation"]
                        or latest.get("parser_binding") != ready_binding
                        or latest.get("parser_attempts") != attempts+1
                        or latest.get("execution_deadline") != current["execution_deadline"]):
                        raise BoardError("document_reader_fence_changed", "Reconcile the original reader", status_code=409)
                    if not (result and result.get("cleanup") == "not_launched"):
                        latest["witness_digest"] = read_witness(final, latest)
                    latest["live_writer"] = None
                    latest["reason"] = result.get("reason") if result else "document_reader_interrupted"
                    if receipt is not None:
                        try:
                            await sources.authority(db, owner, final, latest, adoption_workspace_root)
                            await current_source_root(db, owner, operator)
                            if request_task.cancelling():
                                raise BoardError("document_reader_cancelled_cleanup_only", "Only the original positive cleanup remains authorized", status_code=409)
                            if not self._started or datetime.fromisoformat(latest["execution_deadline"]) <= now():
                                raise BoardError("document_original_execution_window_expired", "Only cleanup remains authorized", status_code=409)
                        except (BoardError, AuthFailure) as exc:
                            adoption_error = exc
                            latest["reason"] = "document_output_cleanup_required"
                        else:
                            latest["evidence"] = receipt
                    final.document_metadata_json = canonical(latest).decode(); final.revision += 1
                    final.metadata_digest = _metadata_digest(final)
                    await db.commit()
            await shield_positive_cleanup(settle())
        if adoption_error is not None:
            # Stale authority already committed cleanup-only. Physical removal
            # happens outside the SQL writer; unknown absence remains charged.
            try:
                remove_unadopted_output(sources.source_path(final, latest, "evidence"), receipt)
            except OSError:
                pass
            else:
                cleanup_revision = final.revision
                await _begin_immediate(db)
                closed, closed_value = await sources.owned(db, owner, identifier, revision=cleanup_revision, capability=CAPABILITY)
                if closed_value != latest or closed_value.get("live_writer") or closed_value.get("evidence"):
                    raise BoardError("document_reader_fence_changed", "Inspect the original cleanup receipt", status_code=409)
                closed_value["reason"] = "document_authority_changed_output_discarded"
                closed.document_metadata_json = canonical(closed_value).decode(); closed.revision += 1
                closed.metadata_digest = _metadata_digest(closed)
                await db.commit()
            raise adoption_error
        # Revalidate current owner/Goal after extraction and before private return.
        final, latest = await sources.owned(db, owner, identifier, capability=CAPABILITY)
        await sources.authority(db, owner, final, latest, dict(root_binding()))
        await current_source_root(db, owner, operator)
        return result
