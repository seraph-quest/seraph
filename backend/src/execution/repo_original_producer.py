"""Private original-producer transport; signatures are not host isolation.

Only the Source owner may issue the live adapter. The signing key exists in the
original supervisor only. Recovery reads must use the canonical registered ready
record, never a public key obtained from a completion file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import asyncio
import fcntl
import hashlib
import json
import math
import re
import os
from pathlib import Path
import secrets
import select
import socket
import stat
import subprocess
import sys
import time
import threading
from typing import Any, Callable
import weakref

DOMAIN = b"seraph.repository.original_producer_completion.v1\0"
PROFILE = "repository.original_producer.v1"
COMPLETION = "repository.original_producer_completion.v1"
MAX_MESSAGE = 16 * 1024
MAX_ENVELOPE = 2 * 1024 * 1024
_OWNERS = weakref.WeakKeyDictionary()
_READIES = weakref.WeakKeyDictionary()
_COMMANDS = weakref.WeakKeyDictionary()
_LIVE_COMPLETIONS = weakref.WeakKeyDictionary()
_ENABLED_SERVICES = weakref.WeakKeyDictionary()
_PHYSICAL_COMPLETIONS = weakref.WeakKeyDictionary()
_PHYSICAL_SCOPES = weakref.WeakKeyDictionary()
_PHYSICAL_GROUPS = weakref.WeakKeyDictionary()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def original_producer_sources():
    directory = Path(__file__).parent
    return {name: digest((directory / name).read_bytes()) for name in (
        "repo_original_producer.py", "repo_original_producer_finalizer.py",
        "repo_supervisor.py", "repo_sandbox.py", "repo_node.py", "repo_worker.py")}


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class OriginalProducerReady:
    admission_digest: str
    public_key: str
    nonce: str
    pid: int
    start_identity: str
    boot_id: str
    directory_identity: tuple[int, int]
    guard_identity: tuple[int, int]

    def projection(self):
        return {name: list(value) if isinstance(value, tuple) else value
                for name in self.__dataclass_fields__ for value in [getattr(self, name)]}


@dataclass(frozen=True, slots=True)
class OriginalProducerRegistrationAck:
    """Returned only AFTER the Source registration transaction commits."""
    registration_digest: str
    ready_digest: str


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class OriginalProducerCommand:
    registration_digest: str
    ordinal: int
    argv_digest: str


@dataclass(frozen=True, slots=True)
class OriginalProducerObservation:
    directory_path: str
    admission_json: str
    guard_path: str
    host_binding_json: str
    pidfd: int


def assert_original_producer_ready(owner, ready):
    observed = _READIES.get(ready) if type(ready) is OriginalProducerReady else None
    if owner not in _OWNERS or observed is None or observed[0] is not owner:
        raise ValueError("actual_original_producer_ready_required")
    poll = select.poll()
    poll.register(observed[1].pidfd, select.POLLIN)
    if poll.poll(0):
        raise ValueError("original_producer_not_live_at_registration")


def original_producer_observation(owner, ready):
    """Only the actual owned Popen/channel observation, never caller paths."""
    assert_original_producer_ready(owner, ready)
    return _READIES[ready][1]


def assert_original_producer_command(owner, command):
    if (type(command) is not OriginalProducerCommand or owner not in _OWNERS
            or _COMMANDS.get(command) is not owner):
        raise ValueError("actual_original_producer_command_required")


def assert_original_producer_live_completion(owner, result):
    """Actual original Popen wait/EOF result; never a copied transport receipt."""
    stored = _LIVE_COMPLETIONS.get(owner) if type(owner) is OriginalProducerOwner else None
    if (stored is None or stored[0] is not result
            or digest(canonical(result.get("original_producer_completion"))) != stored[1]
            or result.get("live_parent_transport") != json.loads(stored[2])):
        raise ValueError("actual_original_producer_live_completion_required")


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _OriginalProducerPhysicalCompletion:
    """Physical facts only; the Source owner supplies current-row authority."""


def original_producer_live_owner(service, jobs, result):
    """Resolve the actual wait-result owner; never revive its closed pidfd."""
    if type(result) is not dict:
        raise ValueError("actual_original_producer_live_completion_required")
    ready = result.get("original_producer_ready")
    observed = _READIES.get(ready) if type(ready) is OriginalProducerReady else None
    owner = observed[0] if observed is not None else None
    owned = _OWNERS.get(owner) if type(owner) is OriginalProducerOwner else None
    if owned is None or owned[0] is not service or owned[1] is not jobs or service.jobs is not jobs:
        raise ValueError("actual_original_producer_live_owner_required")
    assert_original_producer_live_completion(owner, result)
    lane = service._iterative_lanes.get(owner.job_id)
    if (lane is None or lane._descriptor != owner.guard_fd
            or str(lane.lock_path) != owned[2] or str(lane.workspace_root) != owned[3]):
        raise ValueError("original_producer_live_lane_changed")
    metadata = os.fstat(owner.guard_fd)
    if (metadata.st_dev, metadata.st_ino) != ready.guard_identity:
        raise ValueError("original_producer_guard_changed")
    return owner


def _physical_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _assert_original_completion_scope(witness, *, primary_only=False):
    root_ref = _PHYSICAL_GROUPS.get(witness) if type(witness) is _OriginalProducerPhysicalCompletion else None
    primary = root_ref() if root_ref is not None else None
    scope = _PHYSICAL_SCOPES.get(primary) if primary is not None else None
    if (scope is None or witness not in _PHYSICAL_COMPLETIONS
            or primary_only and witness is not primary):
        raise ValueError("actual_original_producer_physical_completion_required")
    if scope["task"] is not _physical_task() or scope["thread"] != threading.get_ident():
        raise ValueError("original_producer_completion_scope_owner_changed")
    owner = scope["owner"]
    if owner is not None:
        owned = _OWNERS.get(owner)
        if (owned is None or owned[0]._iterative_lanes.get(owner.job_id) is not scope["lane"]
                or scope["lane"]._descriptor != scope["owned_descriptor"] or owner.guard_fd != scope["owned_descriptor"]):
            raise ValueError("original_producer_live_lane_changed")
    return scope


def assert_original_producer_completion_scope(witness):
    """Lifetime assertion only: no filesystem or physical reads inside SQL."""
    _assert_original_completion_scope(witness)


def original_producer_completion_result(witness):
    _assert_original_completion_scope(witness)
    stored = _PHYSICAL_COMPLETIONS.get(witness) if type(witness) is _OriginalProducerPhysicalCompletion else None
    if stored is None:
        raise ValueError("actual_original_producer_physical_completion_required")
    descriptor, identity, result, completion_digest, output_digests = stored
    metadata = os.fstat(descriptor)
    if (metadata.st_dev, metadata.st_ino) != identity:
        raise ValueError("original_producer_guard_changed")
    if (digest(canonical(result["original_producer_completion"])) != completion_digest
            or {name: digest(raw) for name, raw in result["outputs"].items()} != output_digests
            or result["manifest"] != result["original_producer_completion"]["manifest"]
            or result["readback"] != result["manifest"]):
        raise ValueError("original_producer_staged_completion_changed")
    return result


def _current_native_host_binding(binding):
    """Observe the same native host boundary used by original registration."""
    from src.execution.repo_sandbox import _open_trusted_directory
    workspace = _open_trusted_directory(Path(binding["workspace_path"]))
    try:
        workspace_stat = os.fstat(workspace)
    finally:
        os.close(workspace)
    machine_fd = os.open("/etc/machine-id", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(machine_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_size > 256:
            raise ValueError("original_producer_host_identity_unavailable")
        machine = os.read(machine_fd, 257)
        after = os.fstat(machine_fd)
        attributes = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (tuple(getattr(before, name) for name in attributes) != tuple(getattr(after, name) for name in attributes)
                or len(machine) != before.st_size or not re.fullmatch(b"[0-9a-f]{32}\\n?", machine)):
            raise ValueError("original_producer_host_identity_changed")
    finally:
        os.close(machine_fd)
    pid_namespace = os.stat("/proc/self/ns/pid")
    mount_namespace = os.stat("/proc/self/ns/mnt")
    proc = os.stat("/proc")
    mount_fd = os.open("/proc/self/mountinfo", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(mount_fd).st_mode):
            raise ValueError("original_producer_proc_mount_unavailable")
        raw = os.read(mount_fd, 131073)
        if len(raw) > 131072:
            raise ValueError("original_producer_proc_mount_bound")
    finally:
        os.close(mount_fd)
    mounts = [line for line in raw.decode().splitlines() if len(line.split()) >= 10
        and line.split()[3:5] == ["/", "/proc"] and line.partition(" - ")[2].split()[:1] == ["proc"]]
    if len(mounts) != 1:
        raise ValueError("original_producer_proc_mount_unavailable")
    return {"schema": "repository.original_producer_host.v1", "machine_digest": digest(machine.strip()),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "pid_namespace": [pid_namespace.st_dev, pid_namespace.st_ino],
        "workspace_path": binding["workspace_path"], "workspace_identity": [workspace_stat.st_dev, workspace_stat.st_ino],
        "guard_path": binding["guard_path"], "guard_identity": binding["guard_identity"],
        "mount_namespace": [mount_namespace.st_dev, mount_namespace.st_ino],
        "proc_identity": [proc.st_dev, proc.st_ino], "proc_mount_sha256": digest(mounts[0].encode())}


def _assert_registered_host(registration, ready):
    host = registration["native_host_binding"]
    if (_current_native_host_binding(host) != host or host["boot_id"] != ready.boot_id
            or host["guard_path"] != registration["guard_path"]
            or host["guard_identity"] != list(ready.guard_identity)
            or registration["producer_sources"] != original_producer_sources()):
        raise ValueError("original_producer_native_host_changed")


def _assert_original_pid_absent(ready):
    from src.execution.repo_supervisor import pidfd_open
    try:
        descriptor = pidfd_open(ready.pid)
    except ProcessLookupError:
        return
    os.close(descriptor)
    raise ValueError("pending_original_producer")


def _read_registered_bundle(registration, ready):
    from src.execution.repo_sandbox import _open_trusted_directory
    directory = _open_trusted_directory(Path(registration["directory_path"]))
    try:
        admission_raw = read_file(directory, "admission.json", MAX_ENVELOPE)
    finally:
        os.close(directory)
    admission = json.loads(admission_raw)
    maximum = admission["original_producer"]["max_output_bytes"]
    if (digest(admission_raw) != registration["admission_digest"]
            or ready.admission_digest != registration["admission_digest"]
            or admission["original_producer"]["sources"] != registration["producer_sources"]
            or type(maximum) is not int or not 0 < maximum <= 16 * 1024 * 1024):
        raise ValueError("original_producer_admission_changed")
    return verify_completion(registration["directory_path"], ready, digest(canonical(registration)),
        maximum_output=maximum, expected_deadline=registration["monotonic_deadline"],
        expected_wall_cutoff=registration["execution_deadline_at"])


def _recovered_result(registration, ready, body, outputs):
    return {"status": body["manifest"]["status"], "manifest": body["manifest"],
        "readback": body["manifest"], "outputs": outputs, "original_producer_completion": body,
        "original_producer_ready": ready,
        "original_producer_registration": OriginalProducerRegistrationAck(digest(canonical(registration)), registration["ready_digest"]),
        "original_producer_directory": registration["directory_path"],
        "cleanup": {"cleanup_proven": True}, "learning": "no_learning", "operator_visible": True}


def stage_original_producer_related_completion(active_primary, registration):
    """Verify another original iteration under this SAME held physical guard."""
    scope = _assert_original_completion_scope(active_primary, primary_only=True)
    original_producer_completion_result(active_primary)
    anchor = json.loads(scope["registration"])
    keys = ("job_id", "repository_attempt_id", "owner_principal_id", "owner_session_id",
        "root_fence", "root_authority_digest", "original_source_digest", "native_binding",
        "native_host_binding", "guard_path", "original_deadline_at", "producer_sources", "source_artifact_digest")
    if (registration.get("schema") != PROFILE or any(registration.get(key) != anchor[key] for key in keys)
            or type(registration.get("iteration_index")) is not int
            or not 1 <= registration["iteration_index"] <= 3
            or registration["iteration_id"] in scope["iterations"] or len(scope["iterations"]) >= 3):
        raise ValueError("original_producer_related_registration_changed")
    from src.workflows.repo_repair_source import iteration_identity
    if (registration["iteration_id"] != iteration_identity(registration["job_id"], registration["repository_attempt_id"],
            registration["native_binding"]["input_digest"], registration["iteration_index"])
            or registration["process_binding"]["repository_job_id"] != registration["job_id"]
            or registration["process_binding"]["repository_attempt_id"] != registration["repository_attempt_id"]
            or registration["process_binding"]["repository_fence"] != registration["root_fence"]
            or registration["process_binding"]["iteration_id"] != registration["iteration_id"]
            or registration["process_binding"]["iteration_index"] != registration["iteration_index"]):
        raise ValueError("original_producer_related_registration_changed")
    values = dict(registration["ready"])
    for name in ("directory_identity", "guard_identity"):
        values[name] = tuple(values[name])
    ready = OriginalProducerReady(**values)
    if ready.guard_identity != tuple(anchor["ready"]["guard_identity"]):
        raise ValueError("original_producer_guard_changed")
    _assert_registered_host(registration, ready)
    _assert_original_pid_absent(ready)
    body, outputs = _read_registered_bundle(registration, ready)
    result = _recovered_result(registration, ready, body, outputs)
    witness = _OriginalProducerPhysicalCompletion()
    _PHYSICAL_COMPLETIONS[witness] = (scope["descriptor"], ready.guard_identity, result,
        digest(canonical(body)), {name: digest(raw) for name, raw in outputs.items()})
    _PHYSICAL_GROUPS[witness] = weakref.ref(active_primary)
    scope["members"].add(witness)
    scope["iterations"].add(registration["iteration_id"])
    return witness


@contextmanager
def stage_original_producer_completion(registration, *, owner=None, result=None):
    """Stage literal same-boot completion under the original guard until CAS.

    The caller must first validate canonical Source registration/current rows.
    This helper neither reconstructs a Popen nor grants execution or SQL writes.
    """
    from src.execution.repo_sandbox import _open_trusted_directory
    from src.execution.repo_supervisor import platform_ready
    platform_ready()
    if registration.get("schema") != PROFILE:
        raise ValueError("original_producer_registration_required")
    live = owner is not None or result is not None
    if live:
        assert_original_producer_live_completion(owner, result)
        owned = _OWNERS[owner]
        if original_producer_live_owner(owned[0], owned[1], result) is not owner:
            raise ValueError("actual_original_producer_live_owner_required")
        ready = result["original_producer_ready"]
        if (ready.projection() != registration["ready"]
                or result["original_producer_registration"].registration_digest != digest(canonical(registration))
                or result["original_producer_directory"] != registration["directory_path"]):
            raise ValueError("original_producer_live_registration_changed")
        descriptor = os.dup(owner.guard_fd)
    else:
        values = dict(registration["ready"])
        for name in ("directory_identity", "guard_identity"):
            values[name] = tuple(values[name])
        ready = OriginalProducerReady(**values)
        descriptor = None
    witness = None
    try:
        _assert_registered_host(registration, ready)
        if not live:
            guard_path = Path(registration["guard_path"])
            parent = _open_trusted_directory(guard_path.parent)
            try:
                descriptor = os.open(guard_path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
            finally:
                os.close(parent)
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                or (metadata.st_dev, metadata.st_ino) != ready.guard_identity):
            raise ValueError("original_producer_guard_changed")
        if not live:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("pending_original_producer") from exc
            # A newly opened pidfd cannot impersonate the old parent's Popen.
            # Any current occupant is conservatively blocked, including reuse.
            _assert_original_pid_absent(ready)
        body, outputs = _read_registered_bundle(registration, ready)
        if live:
            if body != result["original_producer_completion"] or outputs != result["outputs"]:
                raise ValueError("original_producer_live_completion_changed")
            staged = result
        else:
            staged = _recovered_result(registration, ready, body, outputs)
        witness = _OriginalProducerPhysicalCompletion()
        _PHYSICAL_COMPLETIONS[witness] = (descriptor, ready.guard_identity, staged,
            digest(canonical(body)), {name: digest(raw) for name, raw in outputs.items()})
        _PHYSICAL_SCOPES[witness] = {"task": _physical_task(), "thread": threading.get_ident(),
            "descriptor": descriptor, "registration": canonical(registration),
            "owned_descriptor": owner.guard_fd if live else descriptor,
            "owner": owner, "lane": _OWNERS[owner][0]._iterative_lanes[owner.job_id] if live else None,
            "members": weakref.WeakSet([witness]), "iterations": {registration["iteration_id"]}}
        _PHYSICAL_GROUPS[witness] = weakref.ref(witness)
        yield witness
    finally:
        if witness is not None:
            scope = _PHYSICAL_SCOPES.pop(witness, None)
            for member in list(scope["members"]) if scope is not None else [witness]:
                _PHYSICAL_COMPLETIONS.pop(member, None)
                _PHYSICAL_GROUPS.pop(member, None)
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class OriginalProducerOwner:
    job_id: str
    iteration_id: str
    guard_fd: int
    register_ready: Callable = field(repr=False)
    authorize_command: Callable = field(repr=False)


def _enable_original_producer_service(service, jobs):
    """Private Source wiring only, after the adopted ADR package is installed.

    There is deliberately no caller boolean, settings toggle or serialized grant.
    Every issued owner still requires its genuine original v3 Source binding.
    """
    from src.workflows.repo_repair import RepoRepairService
    if type(service) is not RepoRepairService or service.jobs is not jobs:
        raise ValueError("actual_original_producer_service_required")
    original_producer_preflight(service.sandbox)
    _ENABLED_SERVICES[service] = jobs


def original_producer_service_enabled(service, jobs):
    from src.workflows.repo_repair import RepoRepairService
    return type(service) is RepoRepairService and service.jobs is jobs and _ENABLED_SERVICES.get(service) is jobs


def original_producer_preflight(executor):
    """Additional native prerequisite, never a Source admission grant."""
    from src.execution.repo_sandbox import LocalRepoRepairExecutor
    from src.execution.repo_node import NodeRepoRepairExecutor
    from src.execution.repo_supervisor import platform_ready
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if type(executor) not in (LocalRepoRepairExecutor, NodeRepoRepairExecutor):
        raise ValueError("original_producer_native_executor_required")
    platform_ready()
    Ed25519PrivateKey.generate()
    return original_producer_sources()


async def issue_original_producer_owner(service, jobs, job, *, register_ready, authorize_command):
    """Source-only issuer. Activation is owned by the adopted ADR package."""
    from src.workflows.repo_repair import RepoRepairService
    from src.workflows.repo_repair_source import assert_repo_iteration_process_binding, read_repository_inventory
    if (type(service) is not RepoRepairService or service.jobs is not jobs
            or _ENABLED_SERVICES.get(service) is not jobs):
        raise ValueError("original_producer_mode_not_enabled")
    assert_repo_iteration_process_binding(job.iteration_binding, job)
    async with jobs._session() as db:
        run = await jobs._fetch(db, job.job_id)
        if read_repository_inventory(run)["schema"] != "repository.checkpoint_inventory.v3":
            raise ValueError("original_producer_v3_inventory_required")
    lane = service._iterative_lanes.get(job.job_id)
    if lane is None or lane._descriptor is None:
        raise ValueError("original_producer_owned_lane_required")
    owner = OriginalProducerOwner(job.job_id, job.iteration_binding.iteration_id,
        lane._descriptor, register_ready, authorize_command)
    _OWNERS[owner] = (service, jobs, str(lane.lock_path), str(lane.workspace_root))
    return owner


def assert_original_producer_owner(owner, job):
    if (type(owner) is not OriginalProducerOwner or owner not in _OWNERS
            or owner.job_id != job.job_id or job.iteration_binding is None
            or owner.iteration_id != job.iteration_binding.iteration_id):
        raise ValueError("actual_original_producer_owner_required")


def run_original_producer(executor, job, *, stage, payload, posture, owner, observe_process):
    """Live original Popen: register/authorize through the actual Source owner."""
    from dataclasses import asdict
    from src.execution.repo_sandbox import _open_trusted_directory
    from src.execution.repo_supervisor import finish_supervisor, start_identity, pidfd_open
    assert_original_producer_owner(owner, job)
    directory_path = stage.parent / "original-producers" / owner.iteration_id
    directory = _open_trusted_directory(directory_path, create=True)
    metadata = os.fstat(directory)
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(directory)
        raise ValueError("original_producer_directory_not_private")
    guard = os.fstat(owner.guard_fd)
    payload = dict(payload)
    payload["original_producer"] = {"directory": str(directory_path), "nonce": secrets.token_hex(32),
        "sources": original_producer_sources(),
        "max_output_bytes": executor.limits.max_output_bytes,
        "job": {key: value for key, value in asdict(job).items() if key not in {"patch_bytes", "iteration_binding"}},
        "patch_sha256": digest(job.patch_bytes), "posture": posture,
        "config": executor.config.model_dump(mode="json"),
        "stage_identity": [stage.stat().st_dev, stage.stat().st_ino]}
    admission = canonical(payload)
    write_once(directory, "admission.json", admission, maximum=MAX_ENVELOPE)
    os.fsync(directory)
    request = directory_path / "admission.json"
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    process = None
    pidfd = None
    ready = ack = None
    try:
        process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).absolute()),
            str(request), str(child.fileno()), str(owner.guard_fd)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=payload["environment"], start_new_session=True,
            pass_fds=(child.fileno(), owner.guard_fd))
        child.close()
        pidfd = pidfd_open(process.pid)
        observe_process(process)
        expected_start = start_identity(process.pid)
        message = receive(parent, payload["deadline_at"] - 1)
        keys = {"kind", *OriginalProducerReady.__dataclass_fields__}
        if set(message) != keys or message["kind"] != "ready":
            raise ValueError("original_producer_ready_schema")
        values = {key: message[key] for key in OriginalProducerReady.__dataclass_fields__}
        values["directory_identity"] = tuple(values["directory_identity"])
        values["guard_identity"] = tuple(values["guard_identity"])
        ready = OriginalProducerReady(**values)
        if (ready.admission_digest != digest(admission) or ready.pid != process.pid
                or not expected_start or ready.start_identity != expected_start
                or ready.nonce != payload["original_producer"]["nonce"]
                or ready.boot_id != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                or ready.directory_identity != (metadata.st_dev, metadata.st_ino)
                or ready.guard_identity != (guard.st_dev, guard.st_ino)):
            raise ValueError("original_producer_ready_binding")
        namespace = os.stat("/proc/self/ns/pid")
        child_namespace = os.stat(f"/proc/{process.pid}/ns/pid")
        if (namespace.st_dev, namespace.st_ino) != (child_namespace.st_dev, child_namespace.st_ino):
            raise ValueError("original_producer_pid_namespace_changed")
        workspace_fd = _open_trusted_directory(_OWNERS[owner][3])
        try:
            workspace = os.fstat(workspace_fd)
        finally:
            os.close(workspace_fd)
        machine_fd = os.open("/etc/machine-id", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            machine_stat = os.fstat(machine_fd)
            if (not stat.S_ISREG(machine_stat.st_mode) or machine_stat.st_uid != 0
                    or machine_stat.st_size > 256):
                raise ValueError("original_producer_host_identity_unavailable")
            machine = os.read(machine_fd, 257)
            after = os.fstat(machine_fd)
            attributes = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if (tuple(getattr(machine_stat, name) for name in attributes) != tuple(getattr(after, name) for name in attributes)
                    or len(machine) != machine_stat.st_size or not re.fullmatch(b"[0-9a-f]{32}\\n?", machine)):
                raise ValueError("original_producer_host_identity_changed")
        finally:
            os.close(machine_fd)
        host = {"schema": "repository.original_producer_host.v1", "machine_digest": digest(machine.strip()),
            "boot_id": ready.boot_id, "pid_namespace": [namespace.st_dev, namespace.st_ino],
            "workspace_path": _OWNERS[owner][3], "workspace_identity": [workspace.st_dev, workspace.st_ino],
            "guard_path": _OWNERS[owner][2], "guard_identity": list(ready.guard_identity)}
        mount_namespace = os.stat("/proc/self/ns/mnt")
        producer_mount_namespace = os.stat(f"/proc/{process.pid}/ns/mnt")
        if (mount_namespace.st_dev, mount_namespace.st_ino) != (producer_mount_namespace.st_dev, producer_mount_namespace.st_ino):
            raise ValueError("original_producer_mount_namespace_changed")
        mount_fd = os.open("/proc/self/mountinfo", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(mount_fd).st_mode):
                raise ValueError("original_producer_proc_mount_unavailable")
            mount_raw = os.read(mount_fd, 131073)
            if len(mount_raw) > 131072:
                raise ValueError("original_producer_proc_mount_bound")
        finally:
            os.close(mount_fd)
        proc_mounts = [line for line in mount_raw.decode().splitlines()
            if len(line.split()) >= 10 and line.split()[3:5] == ["/", "/proc"]
            and line.partition(" - ")[2].split()[:1] == ["proc"]]
        if len(proc_mounts) != 1:
            raise ValueError("original_producer_proc_mount_unavailable")
        proc = os.stat("/proc")
        host.update(mount_namespace=[mount_namespace.st_dev, mount_namespace.st_ino],
            proc_identity=[proc.st_dev, proc.st_ino], proc_mount_sha256=digest(proc_mounts[0].encode()))
        _READIES[ready] = (owner, OriginalProducerObservation(str(directory_path), admission.decode(),
            _OWNERS[owner][2], canonical(host).decode(), pidfd))
        assert_original_producer_ready(owner, ready)
        ack = owner.register_ready(ready)
        if (type(ack) is not OriginalProducerRegistrationAck
                or ack.ready_digest != digest(canonical(ready.projection()))
                or len(ack.registration_digest) != 64):
            raise ValueError("original_producer_actual_registration_ack_required")
        send(parent, {"kind": "registered", "registration_digest": ack.registration_digest,
                      "ready_digest": ack.ready_digest})
        process.stdin.write((payload["token"] + "\n").encode())
        process.stdin.flush()
        process.stdin.close()
        ordinal = 0
        while True:
            try:
                message = receive(parent, payload["deadline_at"])
            except ValueError as exc:
                if str(exc) != "original_producer_parent_eof":
                    raise
                break
            if (set(message) != {"kind", *OriginalProducerCommand.__dataclass_fields__}
                    or message["kind"] != "command" or type(message["ordinal"]) is not int
                    or message["ordinal"] != ordinal + 1
                    or message["registration_digest"] != ack.registration_digest):
                raise ValueError("original_producer_command_order")
            command = OriginalProducerCommand(message["registration_digest"], message["ordinal"], message["argv_digest"])
            _COMMANDS[command] = owner
            if owner.authorize_command(command) is not True:
                raise ValueError("original_producer_command_authority_denied")
            ordinal += 1
            send(parent, {"kind": "command_ack", **{key: message[key] for key in command.__dataclass_fields__}})
        transport = finish_supervisor(process, deadline=payload["deadline_at"], stream_limit=executor.limits.max_stream_bytes)
        if process.returncode != 0:
            raise ValueError("original_producer_completion_unproven")
        body, outputs = verify_completion(directory_path, ready, ack.registration_digest,
            maximum_output=executor.limits.max_output_bytes, expected_deadline=payload["deadline_at"],
            expected_wall_cutoff=job.execution_deadline_at)
        observe_process(None)
        result = {"status": body["manifest"]["status"], "manifest": body["manifest"],
                "readback": body["manifest"], "outputs": outputs,
                "original_producer_completion": body, "original_producer_ready": ready,
                "original_producer_registration": ack, "original_producer_directory": str(directory_path),
                "live_parent_transport": transport, "cleanup": {"cleanup_proven": True},
                "effective_profile": payload["original_producer"]["posture"],
                "learning": "no_learning", "operator_visible": True}
        _LIVE_COMPLETIONS[owner] = (result, digest(canonical(body)), canonical(transport).decode())
        return result
    finally:
        parent.close()
        child.close()
        os.close(directory)
        if pidfd is not None:
            os.close(pidfd)
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            # A surviving original producer retains its lane/cleanup ownership.
            # Never close live output pipes to manufacture transport proof.
            if process.poll() is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


def read_file(directory, name, maximum):
    if Path(name).name != name:
        raise ValueError("original_producer_filename")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > maximum):
            raise ValueError("original_producer_file_untrusted")
        data = bytearray()
        while len(data) <= before.st_size:
            block = os.read(fd, min(65536, before.st_size + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        after = os.fstat(fd)
        if (len(data) != before.st_size or
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError("original_producer_file_changed")
        return bytes(data)
    finally:
        os.close(fd)


def write_once(directory, name, raw, *, maximum):
    if Path(name).name != name or len(raw) > maximum:
        raise ValueError("original_producer_output_bound")
    temporary = "." + secrets.token_hex(16)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory)
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:offset + 65536])
        os.fsync(fd)
        try:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        except FileExistsError:
            if read_file(directory, name, maximum) != raw:
                raise ValueError("original_producer_immutable_output_changed")
    finally:
        os.close(fd)
        os.unlink(temporary, dir_fd=directory)


def send(control, value):
    raw = canonical(value)
    if len(raw) > MAX_MESSAGE or control.send(raw) != len(raw):
        raise ValueError("original_producer_protocol_bound")


def receive(control, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("original_producer_deadline")
    control.settimeout(remaining)
    raw = control.recv(MAX_MESSAGE + 1)
    if not raw:
        raise ValueError("original_producer_parent_eof")
    if len(raw) > MAX_MESSAGE:
        raise ValueError("original_producer_protocol_bound")
    return json.loads(raw)


class ProducerControl:
    def __init__(self, control, registration_digest, deadline):
        self.control = control
        self.registration_digest = registration_digest
        self.deadline = deadline
        self.ordinal = 0
        self.authorized_commands = 0
        self.interrupted = False
        self.no_spawn = False

    def authorize(self, argv):
        if self.no_spawn or self.interrupted:
            raise ValueError("original_producer_no_spawn")
        self.ordinal += 1
        command = OriginalProducerCommand(self.registration_digest, self.ordinal, digest(canonical(argv)))
        request = {"kind": "command", **{key: getattr(command, key) for key in command.__dataclass_fields__}}
        try:
            send(self.control, request)
            reply = receive(self.control, self.deadline)
            if reply != {"kind": "command_ack", **{key: value for key, value in request.items() if key != "kind"}}:
                raise ValueError("original_producer_command_ack")
            self.authorized_commands += 1
        except (ValueError, OSError):
            self.interrupted = True
            raise

    def parent_gone(self):
        self.control.setblocking(False)
        try:
            raw = self.control.recv(1, socket.MSG_PEEK)
            if not raw:
                self.interrupted = True
        except BlockingIOError:
            pass
        except OSError:
            self.interrupted = True
        return self.interrupted


def verify_completion(directory_path, ready, registration_digest, *, maximum_output, expected_deadline=None,
                      expected_wall_cutoff=None):
    """Read original registered identity only; signature is never self-issued."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    from src.execution.repo_sandbox import _open_trusted_directory
    if type(ready) is not OriginalProducerReady:
        raise ValueError("original_registered_ready_required")
    directory = _open_trusted_directory(directory_path)
    try:
        identity = os.fstat(directory)
        if (identity.st_dev, identity.st_ino) != ready.directory_identity or identity.st_uid != os.getuid() or stat.S_IMODE(identity.st_mode) != 0o700:
            raise ValueError("original_producer_directory_changed")
        envelope = json.loads(read_file(directory, "completion.json", MAX_ENVELOPE))
        if not isinstance(envelope, dict) or set(envelope) != {"completion", "signature"}:
            raise ValueError("original_producer_envelope_schema")
        body = envelope["completion"]
        required = {"schema", "ready", "registration_digest", "admission_digest", "outcome",
                    "manifest", "outputs", "finished_monotonic", "deadline_monotonic",
                    "finished_wall", "execution_wall_cutoff"}
        if (not isinstance(body, dict) or set(body) != required or body["schema"] != COMPLETION or body["ready"] != ready.projection()
                or body["registration_digest"] != registration_digest
                or body["admission_digest"] != ready.admission_digest
                or any(type(body[name]) not in (int, float) or not math.isfinite(body[name])
                       for name in ("finished_monotonic", "deadline_monotonic"))
                or body["finished_monotonic"] >= body["deadline_monotonic"]
                or (expected_deadline is not None and body["deadline_monotonic"] != expected_deadline)):
            raise ValueError("original_producer_completion_binding")
        if expected_wall_cutoff is not None and body["execution_wall_cutoff"] != expected_wall_cutoff:
            raise ValueError("original_producer_wall_cutoff_changed")
        try:
            finished_wall = datetime.fromisoformat(body["finished_wall"].replace("Z", "+00:00"))
            cutoff_wall = datetime.fromisoformat(body["execution_wall_cutoff"].replace("Z", "+00:00"))
            if finished_wall.tzinfo is None or cutoff_wall.tzinfo is None or finished_wall >= cutoff_wall:
                raise ValueError("original_producer_wall_deadline")
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("original_producer_wall_deadline") from exc
        if (body["outcome"] not in {"completed_requested_checks", "completed_requested_check_failure",
                "zero_command_prefix", "interrupted_prefix"}
                or not isinstance(body["manifest"], dict) or not isinstance(body["outputs"], dict)):
            raise ValueError("original_producer_completion_schema")
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(ready.public_key, validate=True))
        try:
            key.verify(base64.b64decode(envelope["signature"], validate=True), DOMAIN + canonical(body))
        except InvalidSignature as exc:
            raise ValueError("original_producer_signature_invalid") from exc
        outputs = {}
        total = 0
        for name, binding in body["outputs"].items():
            if name not in {"manifest.json", "readback.json", "diff.patch", "pytest.stdout", "pytest.stderr", "build.stdout", "build.stderr"}:
                raise ValueError("original_producer_output_name")
            if (not isinstance(binding, dict) or set(binding) != {"sha256", "size_bytes"}
                    or type(binding["size_bytes"]) is not int or binding["size_bytes"] < 0):
                raise ValueError("original_producer_output_schema")
            raw = read_file(directory, name, maximum_output)
            total += len(raw)
            if total > maximum_output or binding != {"sha256": digest(raw), "size_bytes": len(raw)}:
                raise ValueError("original_producer_output_changed")
            outputs[name] = raw
        if (not {"manifest.json", "readback.json", "diff.patch", "pytest.stdout", "pytest.stderr"}.issubset(outputs)
                or outputs["manifest.json"] != outputs["readback.json"]
                or json.loads(outputs["manifest.json"]) != body["manifest"]):
            raise ValueError("original_producer_full_readback_required")
        return body, outputs
    finally:
        os.close(directory)


def child_main(request, control_fd, guard_fd):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from src.execution import repo_supervisor
    from src.execution.repo_sandbox import _open_trusted_directory
    directory = _open_trusted_directory(Path(request).parent)
    job = json.loads(read_file(directory, Path(request).name, MAX_ENVELOPE))
    durable = job["original_producer"]
    if Path(durable["directory"]) != Path(request).parent:
        raise ValueError("original_producer_admission_directory")
    if durable["sources"] != original_producer_sources():
        raise ValueError("original_producer_sources_changed")
    control = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET, fileno=control_fd)
    if (control.getsockopt(socket.SOL_SOCKET, socket.SO_DOMAIN) != socket.AF_UNIX
            or control.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET):
        raise ValueError("original_producer_control_type")
    repo_supervisor.enable_subreaper()
    key = Ed25519PrivateKey.generate()
    metadata = os.fstat(directory)
    guard = os.fstat(guard_fd)
    ready = OriginalProducerReady(digest(canonical(job)),
        base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(),
        durable["nonce"], os.getpid(), repo_supervisor.start_identity(os.getpid()),
        Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        (metadata.st_dev, metadata.st_ino), (guard.st_dev, guard.st_ino))
    send(control, {"kind": "ready", **ready.projection()})
    ack = receive(control, job["deadline_at"] - 1)
    if (set(ack) != {"kind", "registration_digest", "ready_digest"} or ack["kind"] != "registered"
            or ack["ready_digest"] != digest(canonical(ready.projection()))
            or len(ack["registration_digest"]) != 64):
        raise ValueError("original_producer_registration_ack")
    controller = ProducerControl(control, ack["registration_digest"], job["deadline_at"] - 1)
    repo_supervisor.ORIGINAL_PRODUCER = controller
    try:
        repo_supervisor.main(Path(request))
        controller.no_spawn = True
        from src.execution.repo_original_producer_finalizer import finalize_original_outputs
        manifest, outputs, outcome = finalize_original_outputs(job, controller)
        if durable["sources"] != original_producer_sources():
            raise ValueError("original_producer_sources_changed")
        for name, raw in outputs.items():
            write_once(directory, name, raw, maximum=durable["max_output_bytes"])
        # Output directory entries must survive BEFORE the envelope can survive.
        os.fsync(directory)
        finished = time.monotonic()
        if finished >= job["deadline_at"]:
            raise ValueError("original_producer_publication_deadline")
        body = {"schema": COMPLETION, "ready": ready.projection(),
                "registration_digest": ack["registration_digest"], "admission_digest": ready.admission_digest,
                "outcome": outcome, "manifest": manifest,
                "outputs": {name: {"sha256": digest(raw), "size_bytes": len(raw)} for name, raw in outputs.items()},
                "finished_monotonic": finished, "deadline_monotonic": job["deadline_at"],
                "finished_wall": datetime.now(timezone.utc).isoformat(),
                "execution_wall_cutoff": durable["job"]["execution_deadline_at"]}
        cutoff = datetime.fromisoformat(body["execution_wall_cutoff"].replace("Z", "+00:00"))
        if cutoff.tzinfo is None or datetime.fromisoformat(body["finished_wall"]) >= cutoff:
            raise ValueError("original_producer_wall_deadline")
        encoded = canonical({"completion": body,
            "signature": base64.b64encode(key.sign(DOMAIN + canonical(body))).decode()})
        write_once(directory, "completion.json", encoded, maximum=MAX_ENVELOPE)
        os.fsync(directory)
        return 0
    finally:
        controller.no_spawn = True
        control.close()
        os.close(directory)
        os.close(guard_fd)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(child_main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
