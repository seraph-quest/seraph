"""Private fixed-publication supervisor; independent child lifetime, no authority store.

Linux supervision is optional host execution, never an OS isolation claim. The
backend authorizes every fixed next command; parent loss stops that stream while
this helper continues owning the already admitted child, pipes and inherited lock.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import time

MAX_REQUEST = 8 * 1024 * 1024
MAX_PROOF = 2 * 1024 * 1024
MAX_MESSAGE = 64 * 1024
PROFILE = "repo-publication-supervisor-v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def identity():
    from src.execution.repo_supervisor import platform_ready
    from src.execution.repo_publication import PublicationError
    try:
        platform_ready()
    except (OSError, ValueError) as exc:
        raise PublicationError("publication_linux_supervisor_unavailable") from exc
    paths = {
        "helper_sha256": Path(__file__),
        "producer_sha256": Path(__file__).with_name("repo_publication.py"),
        "native_supervisor_sha256": Path(__file__).with_name("repo_supervisor.py"),
        "worker_sha256": Path(__file__).with_name("repo_worker.py"),
        "interpreter_sha256": Path(sys.executable).resolve(),
    }
    return {"profile": PROFILE, "platform": "linux-x86_64", "isolation_claim": "none",
            "interpreter_path": str(Path(sys.executable).absolute()),
            **{key: sha(path.read_bytes()) for key, path in paths.items()}}


def _directory(stage):
    from src.execution.repo_sandbox import _open_trusted_directory
    root = stage.parent / "supervisor"
    descriptor = _open_trusted_directory(root, create=True)
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(descriptor)
        raise ValueError("publication_supervisor_directory_untrusted")
    return root, descriptor


def _read(directory, name, maximum):
    if Path(name).name != name:
        raise ValueError("publication_supervisor_filename_invalid")
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1 or before.st_size > maximum:
            raise ValueError("publication_supervisor_file_untrusted")
        raw = bytearray()
        while len(raw) <= before.st_size:
            chunk = os.read(descriptor, min(65536, before.st_size + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("publication_supervisor_file_changed")
        return bytes(raw)
    finally:
        os.close(descriptor)


def _write(directory, name, raw, *, replace=False):
    if Path(name).name != name or len(raw) > MAX_REQUEST:
        raise ValueError("publication_supervisor_output_invalid")
    temporary = "." + secrets.token_hex(16) + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        cursor = 0
        while cursor < len(raw):
            cursor += os.write(descriptor, raw[cursor:cursor + 65536])
        os.fsync(descriptor)
        if replace:
            try:
                _read(directory, name, MAX_PROOF)
            except FileNotFoundError:
                pass
            os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            try:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                if _read(directory, name, MAX_REQUEST) != raw:
                    raise ValueError("publication_supervisor_output_changed")
        os.fsync(directory)
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


@contextmanager
def guard(stage):
    """Nonblocking private lock. Its absence is never terminal proof."""
    import fcntl
    root, directory = _directory(stage)
    descriptor = os.open("producer.guard", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
            raise ValueError("publication_producer_guard_untrusted")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("publication_producer_still_live") from exc
        current = os.stat("producer.guard", dir_fd=directory, follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("publication_producer_guard_changed")
        yield root, directory, descriptor
    finally:
        # Do not explicitly unlock: inherited descriptors must retain the OFD
        # lock when the request parent dies or unwinds before its child exits.
        os.close(descriptor)
        os.close(directory)


@dataclass
class Admission:
    payload: dict
    path: Path
    digest: str
    directory: int
    guard_fd: int

    def checkpoint(self):
        return {"schema": PROFILE, "admission_path": str(self.path), "admission_sha256": self.digest,
                "binding": self.payload["binding"],
                "runtime": self.payload["runtime"], "stage": self.payload["stage"],
                "deadline_at": self.payload["deadline_at"], "guard_identity": self.payload["guard_identity"]}


def admit(stage, source, preview, patch, binding, *, directory, guard_fd, seconds_limit=30):
    """Capture approved fixed inputs before the canonical producer checkpoint."""
    from src.execution.repo_publication import digest
    runtime = identity()
    if type(seconds_limit) not in {float, int} or not 0 < seconds_limit <= 30:
        raise ValueError("publication_supervisor_deadline")
    if preview.get("local_posture", {}).get("supervisor") != runtime:
        raise ValueError("publication_approved_supervisor_changed")
    metadata = os.fstat(guard_fd)
    payload = {"schema": PROFILE, "stage": str(stage.absolute()), "source": str(source.root.absolute()),
               "preview": preview, "preview_digest": digest(preview), "patch": base64.b64encode(patch).decode(),
               "patch_sha256": sha(patch), "binding": binding, "runtime": runtime,
               "token": secrets.token_hex(32), "deadline_at": time.monotonic() + seconds_limit,
               "guard_identity": [metadata.st_dev, metadata.st_ino]}
    raw = canonical(payload)
    if len(raw) > MAX_REQUEST:
        raise ValueError("publication_supervisor_input_limit")
    name = sha(raw) + ".admission.json"
    _write(directory, name, raw)
    return Admission(payload, stage.parent / "supervisor" / name, sha(raw), directory, guard_fd)


def _send(control, value):
    raw = canonical(value)
    if len(raw) > MAX_MESSAGE or control.send(raw) != len(raw):
        raise ValueError("publication_supervisor_protocol_limit")


def _receive(control, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("publication_supervisor_deadline")
    control.settimeout(remaining)
    raw = control.recv(MAX_MESSAGE + 1)
    if not raw:
        raise EOFError("publication_request_parent_gone")
    if len(raw) > MAX_MESSAGE:
        raise ValueError("publication_supervisor_protocol_limit")
    return json.loads(raw)


def run(admission, authorize, *, process_observer=None):
    """Only the canonical caller can acknowledge each authority-sensitive step."""
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    deadline = admission.payload["deadline_at"]
    process = subprocess.Popen([admission.payload["runtime"]["interpreter_path"], "-I", str(Path(__file__).absolute()),
                                str(admission.path), str(child.fileno()), str(admission.guard_fd)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               pass_fds=(child.fileno(), admission.guard_fd), start_new_session=True,
                               env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"})
    child.close()
    try:
        while True:
            try:
                message = _receive(parent, deadline)
            except EOFError:
                break
            if message.get("kind") == "authorize":
                authorize()
                _send(parent, {"kind": "authorized", "nonce": message["nonce"], "token": admission.payload["token"]})
            elif message.get("kind") == "started":
                if process_observer is not None:
                    process_observer(message)
                _send(parent, {"kind": "started_seen", "nonce": message["nonce"]})
            elif message.get("kind") == "terminal":
                break
            else:
                raise ValueError("publication_supervisor_protocol_invalid")
        process.wait(timeout=max(.001, deadline - time.monotonic()))
        if process.returncode != 0:
            diagnostic = process.stderr.read(1024).decode(errors="replace")
            raise ValueError("publication_supervisor_failed:" + diagnostic)
        proof = terminal(admission.checkpoint())
        if process.returncode != 0 or proof["status"] != "complete":
            raise ValueError(proof.get("reason") or "publication_producer_incomplete")
        return proof["result"] | {"supervisor_proof": proof, "supervisor_proof_sha256": sha(canonical(proof))}
    finally:
        # Parent loss stops new commands. This caller never signals an observed
        # or recovered PID; the directly owning supervisor retains cleanup.
        parent.close()
        process.stderr.close()


def stage_digest(stage):
    from src.execution.repo_worker import _walk_tree, _open_source_regular_file
    records = []
    total = 0
    for relative, _, directory, metadata in _walk_tree(stage):
        if directory:
            continue
        descriptor, initial = _open_source_regular_file(stage, relative, expected_stat=metadata)
        try:
            if initial.st_size > 2 * 1024 * 1024:
                raise ValueError("publication_supervisor_stage_file_limit")
            raw = bytearray()
            while len(raw) <= initial.st_size:
                chunk = os.read(descriptor, min(65536, initial.st_size + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(descriptor)
            if len(raw) != initial.st_size or (initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("publication_supervisor_stage_changed")
            total += len(raw)
            if total > 128 * 1024 * 1024 or len(records) >= 4096:
                raise ValueError("publication_supervisor_stage_limit")
            records.append({"path": relative, "sha256": sha(raw), "size": len(raw), "mode": stat.S_IMODE(initial.st_mode)})
        finally:
            os.close(descriptor)
    return {"sha256": sha(canonical(sorted(records, key=lambda item: item["path"]))), "files": len(records), "bytes": total}


def terminal(checkpoint):
    """Verify actual immutable proof and output; caller rows cannot invent it."""
    from src.execution.repo_sandbox import _open_trusted_directory
    path = Path(checkpoint["admission_path"])
    directory = _open_trusted_directory(path.parent)
    try:
        raw = _read(directory, path.name, MAX_REQUEST)
        if sha(raw) != checkpoint["admission_sha256"]:
            raise ValueError("publication_supervisor_admission_changed")
        admission = json.loads(raw)
        for key in ("binding", "runtime", "stage", "deadline_at", "guard_identity"):
            if admission.get(key) != checkpoint.get(key):
                raise ValueError("publication_supervisor_admission_changed")
        if admission["runtime"] != identity():
            raise ValueError("publication_supervisor_runtime_changed")
        proof = json.loads(_read(directory, admission["token"] + ".terminal.json", MAX_PROOF))
        if proof.get("schema") != PROFILE or proof.get("admission_sha256") != sha(raw) or proof.get("token") != admission["token"] or proof.get("binding") != admission["binding"] or proof.get("runtime") != admission["runtime"] or proof.get("quiescent") is not True or proof.get("cleanup", {}).get("oracle") != "linux_subreaper_waitpid_echild":
            raise ValueError("publication_supervisor_terminal_unproven")
        commands = proof.get("commands")
        if not isinstance(commands, list) or not commands or any(item.get("direct_reaped") is not True or item.get("output_drained") is not True or item.get("group_empty") is not True for item in commands):
            raise ValueError("publication_supervisor_terminal_unproven")
        if stage_digest(Path(admission["stage"])) != proof.get("stage_output"):
            raise ValueError("publication_supervisor_output_changed")
        return proof
    except FileNotFoundError as exc:
        raise ValueError("publication_supervisor_terminal_missing") from exc
    finally:
        os.close(directory)


def main(path, control_fd, guard_fd):
    # Insert only this server-owned backend, never the repository being repaired.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.execution.repo_supervisor import enable_subreaper, cleanup, start_identity
    from src.execution.repo_publication import SourceGit, digest, _produce_direct
    from src.execution.repo_sandbox import _open_trusted_directory
    enable_subreaper()
    directory = _open_trusted_directory(path.parent)
    control = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET, fileno=control_fd)
    if (control.getsockopt(socket.SOL_SOCKET, socket.SO_DOMAIN) != socket.AF_UNIX
            or control.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET):
        raise ValueError("publication_supervisor_control_socket_invalid")
    raw = _read(directory, path.name, MAX_REQUEST)
    admission = json.loads(raw)
    runtime = identity()
    metadata = os.fstat(guard_fd)
    if admission.get("schema") != PROFILE or admission.get("runtime") != runtime or admission.get("guard_identity") != [metadata.st_dev, metadata.st_ino] or len(admission.get("token", "")) != 64 or digest(admission["preview"]) != admission["preview_digest"]:
        raise ValueError("publication_supervisor_admission_invalid")
    patch = base64.b64decode(admission["patch"], validate=True)
    if sha(patch) != admission["patch_sha256"] or len(patch) > 2 * 1024 * 1024:
        raise ValueError("publication_supervisor_patch_changed")
    deadline = float(admission["deadline_at"])
    if not 0 < deadline - time.monotonic() <= 30:
        raise ValueError("publication_supervisor_deadline")
    commands = []
    supervisor = {"pid": os.getpid(), "start": start_identity(os.getpid()),
                  "boot_id": Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    if not supervisor["start"]:
        raise ValueError("publication_supervisor_identity_unavailable")

    def authorize():
        nonce = secrets.token_hex(16)
        _send(control, {"kind": "authorize", "nonce": nonce})
        reply = _receive(control, deadline)
        if reply != {"kind": "authorized", "nonce": nonce, "token": admission["token"]}:
            raise ValueError("publication_supervisor_authorization_missing")

    def observe(record):
        if record["phase"] == "started":
            progress = {"schema": PROFILE, "admission_sha256": sha(raw), "token": admission["token"],
                        "supervisor": supervisor, "command": record, "binding": admission["binding"]}
            _write(directory, admission["token"] + ".progress.json", canonical(progress), replace=True)
            nonce = secrets.token_hex(16)
            try:
                _send(control, {"kind": "started", "nonce": nonce, **record})
                reply = _receive(control, deadline)
                if reply != {"kind": "started_seen", "nonce": nonce}:
                    raise ValueError("publication_supervisor_protocol_invalid")
            except (EOFError, BrokenPipeError):
                # This admitted child has fixed finite inputs and may naturally
                # finish. The next authorize() will reject the absent parent.
                pass
        else:
            commands.append(record)

    result = None
    reason = None
    try:
        result = _produce_direct(Path(admission["stage"]), SourceGit(Path(admission["source"])), admission["preview"], patch, authorize,
                                 deadline_at=deadline, guard_fd=guard_fd, process_observer=observe)
    except (ValueError, OSError, EOFError) as exc:
        reason = str(exc)[:256] or type(exc).__name__
    finally:
        proof = cleanup(deadline)
    marker = {"schema": PROFILE, "admission_sha256": sha(raw), "token": admission["token"],
              "binding": admission["binding"], "runtime": runtime, "supervisor": supervisor,
              "status": "complete" if result is not None and reason is None else "prefix_complete",
              "reason": reason, "result": result, "commands": commands, "cleanup": proof,
              "quiescent": proof.get("cleanup_proven") is True,
              "stage_output": stage_digest(Path(admission["stage"])), "finished_monotonic": time.monotonic()}
    encoded = canonical(marker)
    if len(encoded) > MAX_PROOF or time.monotonic() > deadline or not marker["quiescent"]:
        raise ValueError("publication_supervisor_terminal_unproven")
    _write(directory, admission["token"] + ".terminal.json", encoded)
    try:
        _send(control, {"kind": "terminal"})
    except BrokenPipeError:
        pass
    control.close()
    os.close(directory)
    os.close(guard_fd)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(2)
    try:
        raise SystemExit(main(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])))
    except Exception as exc:
        # No caller supplied rows or missing marker can masquerade as proof.
        print(type(exc).__name__ + ": " + str(exc)[:512], file=sys.stderr)
        raise SystemExit(2)
