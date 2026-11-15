"""Internal one-package controller behind native admission, with actual reap proof."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import subprocess
import sys
import time

from src.execution.process_reap import (ProcessReapError, ReapOutcome, kill_then_reap,
    terminate_then_reap)
from src.execution.tool_package_profile import (MAX_SECONDS, MAX_STREAM, PROFILE,
    ToolPackageBlocked, canonical, digest, expected_output, inspect_runtime,
    read_private, source_package)

# Cleanup time granted to a signalled supervisor before SIGKILL. It admits no
# renewed package work.
CLEANUP_GRACE_SECONDS = .5


def _supervisor_sender(process: subprocess.Popen, pid_start: str | None):
    """Signal only the exact recorded supervisor; no identity, no signal."""
    from src.execution import repo_supervisor

    def send(signum: int) -> bool:
        return bool(pid_start) and repo_supervisor.exact_signal(process.pid, pid_start, signum)
    return send


def kill_and_reap_supervisor(process: subprocess.Popen, pid_start: str | None) -> ReapOutcome:
    """SIGKILL the exact supervisor past its cleanup deadline and reap it (#1212)."""
    try:
        return kill_then_reap(process, send_signal=_supervisor_sender(process, pid_start))
    except ProcessReapError as exc:
        return exc.outcome


def terminate_and_reap_supervisor(process: subprocess.Popen, pid_start: str | None, *,
                                  kill_at: float) -> ReapOutcome:
    """SIGTERM the exact supervisor, SIGKILL it at ``kill_at`` if still running, and reap it.

    ``kill_at`` is the moment the deadline path would kill it
    (``deadline_at`` plus the cleanup grace), so a failure grants the
    supervisor's own SIGTERM cleanup exactly the time it already had and adds
    no new bound. The wait is blocking; async callers run it in a thread.
    """
    try:
        return terminate_then_reap(process, send_signal=_supervisor_sender(process, pid_start),
                                   grace_seconds=max(0.0, kill_at-time.time()))
    except ProcessReapError as exc:
        return exc.outcome


async def _reap_in_owner(reap, *args, **kwargs) -> tuple[ReapOutcome, bool]:
    """Run a blocking reap helper in a thread and return only once it finished.

    The event loop is not blocked while the killed supervisor is collected. A
    cancellation that arrives meanwhile is remembered, not obeyed, so the owner
    never abandons its child. The wait is bounded: by the kill moment the
    helper already uses, then by ``KILL_REAP_LIVENESS_SECONDS`` after SIGKILL
    (an unreapable child yields ``kill_delivered_unreaped``, so cleanup stays
    unproven). Returns the outcome and whether a cancel arrived.
    """
    reaping = asyncio.ensure_future(asyncio.to_thread(reap, *args, **kwargs))
    cancelled = False
    while True:
        try:
            return await asyncio.shield(reaping), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if reaping.done():
                return reaping.result(), cancelled


def private_write(root: Path, name: str, raw: bytes):
    if name not in {"request.json", "package.py", "input.json", "result.json"}:
        raise ToolPackageBlocked("tool_package_private_slot_invalid")
    descriptor = os.open(root/name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(raw)
        destination.flush()
        os.fsync(destination.fileno())


def prepare(stage: Path, runtime_root: Path, *, raw: bytes, job_id: str, fence: int,
            deadline: datetime, preflight: bool = False, _attack_package: bytes | None = None,
            reviewed_adapter=None):
    """Only an internal OS-proof call may select adversarial package bytes.

    Production native admission never accepts this argument or a source path.
    All selected code still runs through the identical enforced profile.
    """
    from src.execution.repo_sandbox import _open_trusted_directory
    metadata = inspect_runtime(runtime_root)
    if reviewed_adapter is not None:
        from src.extensions.authored_adapter import AuthoredAdapter
        if type(reviewed_adapter) is not AuthoredAdapter or preflight or _attack_package is not None:
            raise ToolPackageBlocked("tool_package_reviewed_adapter_invalid")
        reviewed_adapter.input(raw)
    elif not preflight and _attack_package is None:
        expected_output(raw)
    remaining = deadline.astimezone(timezone.utc).timestamp()-time.time()
    if not 0 < remaining <= MAX_SECONDS:
        raise ToolPackageBlocked("tool_package_deadline_invalid")
    if type(fence) is not int or fence < 1 or not job_id or len(job_id) > 128:
        raise ToolPackageBlocked("tool_package_identity_invalid")
    stage.mkdir(mode=0o700)
    descriptor = _open_trusted_directory(stage)
    os.close(descriptor)
    output = stage/"out"
    output.mkdir(mode=0o700)
    package = (reviewed_adapter.code if reviewed_adapter is not None else
        source_package().read_bytes() if _attack_package is None else _attack_package)
    if len(package) > 32768 or len(raw) > 32768:
        raise ToolPackageBlocked("tool_package_staging_limit")
    token = secrets.token_hex(32)
    request = {"schema": 1, "profile": PROFILE, "job_id": job_id, "fence": fence,
        "token": token, "deadline_at": deadline.astimezone(timezone.utc).timestamp(),
        "runtime_root": str(runtime_root), "runtime_digest": metadata["runtime_digest"],
        "package_sha256": digest(package), "input_sha256": digest(raw), "preflight": preflight}
    private_write(stage, "package.py", package)
    private_write(stage, "input.json", raw)
    private_write(output, "result.json", b"")
    private_write(stage, "request.json", canonical(request))
    return request, {**metadata, "package_sha256": digest(package)}


async def execute(stage: Path, request: dict, *, before_dispatch):
    """Start a trusted supervisor; release package work only after actual CAS.

    The callback binds the actual process identity in canonical native storage.
    Cancellation waits for this owning process and its durable cleanup oracle.
    """
    from src.execution.repo_supervisor import exact_signal, start_identity
    deadline = request["deadline_at"]
    supervisor = Path(__file__).with_name("tool_package_supervisor.py")
    process = subprocess.Popen([sys.executable, "-I", "-B", str(supervisor), str(stage/"request.json")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, pass_fds=(), start_new_session=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    pid_start = start_identity(process.pid)
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    released = False
    cancellation = False
    admission_failed = False
    reason = None
    reap: ReapOutcome | None = None
    try:
        while process.poll() is None or selector.get_map():
            if not released and not admission_failed and process.poll() is None:
                try:
                    identity = json.loads(read_private(stage, "out/process-started.json", 4096))
                except FileNotFoundError:
                    identity = None
                if identity is not None:
                    if (identity.get("job_id") != request["job_id"] or identity.get("fence") != request["fence"]
                        or identity.get("token") != request["token"] or identity.get("supervisor_pid") != process.pid
                        or identity.get("supervisor_start") != pid_start):
                        raise ToolPackageBlocked("tool_package_process_identity_changed")
                    try:
                        remaining = request["deadline_at"]-time.time()
                        if remaining <= 0:
                            raise ToolPackageBlocked("tool_package_deadline")
                        async with asyncio.timeout(remaining):
                            await before_dispatch(identity)
                        if time.time() >= request["deadline_at"]:
                            raise ToolPackageBlocked("tool_package_deadline")
                        process.stdin.write((request["token"]+"\n").encode())
                        process.stdin.flush()
                        process.stdin.close()
                        released = True
                    except (Exception, asyncio.CancelledError) as exc:
                        # A rejected CAS never releases package execution. Keep
                        # owning the real helper until its cleanup is read back.
                        cancellation = isinstance(exc, asyncio.CancelledError)
                        admission_failed = True
                        reason = "tool_package_cancel_requested" if cancellation else "tool_package_admission_rejected"
                        if not process.stdin.closed:
                            process.stdin.close()
                        if pid_start:
                            exact_signal(process.pid, pid_start, signal.SIGTERM)
            if time.time() >= deadline:
                reason = "tool_package_deadline"
                if pid_start:
                    exact_signal(process.pid, pid_start, signal.SIGTERM)
                # This is cleanup time only; it admits no renewed package work.
                deadline = min(deadline+CLEANUP_GRACE_SECONDS, time.time()+CLEANUP_GRACE_SECONDS)
                if request["deadline_at"]+CLEANUP_GRACE_SECONDS <= time.time():
                    # The owner reaps the supervisor it kills (#1212).
                    reap, cancelled_during_reap = await _reap_in_owner(
                        kill_and_reap_supervisor, process, pid_start)
                    if cancelled_during_reap:
                        cancellation = True
                    break
            for event, _ in selector.select(timeout=0):
                chunk = os.read(event.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(event.fileobj)
                    continue
                target = buffers[event.data]
                available = MAX_STREAM-len(target)
                target.extend(chunk[:max(0, available)])
                if len(chunk) > available:
                    reason = "tool_package_supervisor_stream_limit"
                    if pid_start:
                        exact_signal(process.pid, pid_start, signal.SIGTERM)
            try:
                await asyncio.sleep(.01)
            except asyncio.CancelledError:
                cancellation = True
                reason = "tool_package_cancel_requested"
                if pid_start:
                    exact_signal(process.pid, pid_start, signal.SIGTERM)
        if process.poll() is None or (reap is not None and reap.status != "exited_before_kill"):
            # Never infer quiescence from the controller leaving its loop, nor
            # from a supervisor that was killed before its own cleanup readback.
            return {"status": "unknown", "reason": "tool_package_cleanup_unproven", "cleanup_proven": False,
                    "supervisor_reap": reap.as_record() if reap is not None else None}
        process.wait()
        try:
            result = json.loads(read_private(stage, "out/supervisor-result.json", 32768))
        except (OSError, ValueError):
            return {"status": "unknown", "reason": reason or "tool_package_supervisor_result_missing", "cleanup_proven": False}
        if any(result.get(key) != request[key] for key in
               ("job_id", "fence", "token", "runtime_digest", "package_sha256", "input_sha256")) or result.get("supervisor_pid") != process.pid or result.get("supervisor_start") != pid_start:
            raise ToolPackageBlocked("tool_package_process_result_changed")
        if reason:
            result["reason"] = reason
        if reap is not None:
            # exited_before_kill: the supervisor finished its own readback.
            result["supervisor_reap"] = reap.as_record()
        result["status"] = ("cancelled" if cancellation else
            "succeeded" if result.get("exit_code") == 0 and not result.get("reason") else "failed") if result.get("cleanup_proven") else "unknown"
        result["no_learning"] = True
        return result
    except (Exception, asyncio.CancelledError) as exc:
        if process.poll() is None:
            # The owner terminates and reaps its supervisor on every failure
            # (#1212), killing it no later than the deadline path would. No
            # package work can be released once stdin is closed.
            if not process.stdin.closed:
                process.stdin.close()
            # Keep reading (and discarding) the supervisor's output while it
            # cleans up, so a full pipe never turns its clean exit into a kill.
            loop = asyncio.get_running_loop()
            drained = []
            try:
                for key in list(selector.get_map().values()):
                    descriptor = key.fileobj.fileno()

                    def discard(descriptor=descriptor):
                        try:
                            chunk = os.read(descriptor, 65536)
                        except BlockingIOError:
                            return
                        except OSError:
                            chunk = b""
                        if not chunk:
                            loop.remove_reader(descriptor)
                    try:
                        loop.add_reader(descriptor, discard)
                    except (NotImplementedError, OSError, ValueError):
                        continue  # no drain for this stream; the reap still runs
                    drained.append(descriptor)
                failed, cancel_requested = await _reap_in_owner(
                    terminate_and_reap_supervisor, process, pid_start,
                    kill_at=request["deadline_at"]+CLEANUP_GRACE_SECONDS)
            except Exception as reap_error:
                # The original failure stays the error; the reap error rides along.
                exc.supervisor_reap = {"status": "reap_error", "error": repr(reap_error)}
                raise exc
            finally:
                for descriptor in drained:
                    loop.remove_reader(descriptor)
            exc.supervisor_reap = failed.as_record()
            if cancel_requested:
                # A cancel that arrived during the reap is recorded; the typed
                # failure still reaches the caller, as it did before the reap.
                exc.cancel_requested = True
        raise
    finally:
        if process.poll() is None and pid_start:
            # Paths that bypass the reap above (e.g. GeneratorExit when the
            # coroutine is closed) still ask the supervisor to clean up.
            exact_signal(process.pid, pid_start, signal.SIGTERM)
        selector.close()
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()
