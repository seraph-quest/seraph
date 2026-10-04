"""Exclusive supervisor for one fixed isolated JSON package.

Reuses the existing Linux subreaper/pidfd cleanup oracle. No public command API
or package-supplied argv exists; native admission owns the private start token.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

# Only server-owned imports; the staged package never becomes an import root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.execution import repo_supervisor as processes
from src.execution.tool_package_profile import (
    MAX_STREAM, PROFILE, digest, inspect_runtime, read_private,
)
from src.execution.repo_worker import _write_private_output

CANCELLED = False


def cancelled(signum, frame):
    global CANCELLED
    CANCELLED = True


def run_isolated(argv, deadline):
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, close_fds=True, pass_fds=(), start_new_session=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    # No source, secret, log or application FD is used as stdin.
    process.stdin.close()
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    reason = None
    code = None
    cleanup = None
    execution_stop = deadline - min(.5, max(.05, (deadline-time.monotonic())*.1))
    try:
        while selector.get_map() or code is None:
            code = process.poll()
            if cleanup is None and (code is not None or CANCELLED or reason or time.monotonic() >= execution_stop):
                reason = reason or ("tool_package_cancelled" if CANCELLED else
                    "tool_package_deadline" if code is None else None)
                cleanup = processes.cleanup(deadline)
                if not cleanup["cleanup_proven"]:
                    reason = "tool_package_cleanup_unproven"
                    break
                if code is None:
                    code = -signal.SIGKILL
                process.returncode = code
            if time.monotonic() >= deadline:
                reason = "tool_package_cleanup_unproven"
                break
            for event, _ in selector.select(timeout=min(.01, max(0, deadline-time.monotonic()))):
                chunk = os.read(event.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(event.fileobj)
                    continue
                target = buffers[event.data]
                available = MAX_STREAM-len(target)
                target.extend(chunk[:max(0, available)])
                if len(chunk) > available:
                    reason = "tool_package_stream_limit"
        return {"exit_code": code, "reason": reason, "stdout": bytes(buffers["stdout"]).decode("utf-8", "replace"),
            "stderr": bytes(buffers["stderr"]).decode("utf-8", "replace"), "process_cleanup": cleanup,
            "cleanup_proven": bool(cleanup and cleanup.get("cleanup_proven"))}
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def main(request_file):
    processes.enable_subreaper()
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    stage = request_file.parent
    request = json.loads(read_private(stage, request_file.name, 16384))
    required = {"schema", "profile", "job_id", "fence", "token", "deadline_at", "runtime_root", "runtime_digest",
                "package_sha256", "input_sha256", "preflight"}
    if (set(request) != required or request["schema"] != 1 or request["profile"] != PROFILE
        or type(request["fence"]) is not int or request["fence"] < 1
        or not isinstance(request["token"], str) or len(request["token"]) != 64
        or not isinstance(request["job_id"], str) or len(request["job_id"]) > 128
        or type(request["preflight"]) is not bool):
        raise ValueError("tool_package_supervisor_request_invalid")
    deadline = float(request["deadline_at"])
    if not time.time() < deadline <= time.time()+10:
        raise ValueError("tool_package_supervisor_deadline_invalid")
    monotonic_deadline = time.monotonic()+deadline-time.time()
    output = stage/"out"
    identity = {"profile": PROFILE, "job_id": request["job_id"], "fence": request["fence"],
        "token": request["token"], "supervisor_pid": os.getpid(),
        "supervisor_start": processes.start_identity(os.getpid()), "no_learning": True}
    _write_private_output(output, "process-started.json", json.dumps(identity, sort_keys=True).encode())
    current = None
    try:
        # Persisted native admission must acknowledge this exact actual process.
        selector = selectors.DefaultSelector()
        selector.register(sys.stdin, selectors.EVENT_READ)
        try:
            while not CANCELLED and time.monotonic() < monotonic_deadline:
                if selector.select(timeout=.01):
                    token = sys.stdin.buffer.readline(66)
                    if token != (request["token"]+"\n").encode():
                        raise ValueError("tool_package_dispatch_barrier_invalid")
                    break
            else:
                raise ValueError("tool_package_dispatch_barrier_expired")
        finally:
            selector.close()
        runtime_root = Path(request["runtime_root"])
        current = inspect_runtime(runtime_root)
        package = read_private(stage, "package.py", 32768)
        inputs = read_private(stage, "input.json", 32768)
        if (current["runtime_digest"] != request["runtime_digest"]
            or digest(package) != request["package_sha256"] or digest(inputs) != request["input_sha256"]):
            raise ValueError("tool_package_dispatch_digest_changed")
        # Root, all runtime files and input/package are read-only; only this
        # precreated output inode is writable. No writable directory exists.
        argv = [str(runtime_root/"bwrap"), "--unshare-all", "--die-with-parent", "--new-session",
            "--cap-drop", "ALL", "--clearenv", "--ro-bind", str(runtime_root/"rootfs"), "/",
            "--ro-bind", str(stage/"input.json"), "/input.json",
            "--ro-bind", str(stage/"package.py"), "/package.py",
            "--bind", str(output/"result.json"), "/out/result.json",
            "--proc", "/proc", "--remount-ro", "/proc", "--remount-ro", "/", "--chdir", "/",
            "--", "/runtime/bin/isolated-python"]
        if request["preflight"]:
            argv.append("preflight")
        result = run_isolated(argv, monotonic_deadline)
    except (OSError, ValueError, RuntimeError):
        proof = processes.cleanup(max(monotonic_deadline, time.monotonic()+.05))
        result = {"exit_code": None, "reason": "tool_package_cancelled_before_dispatch" if CANCELLED else "tool_package_dispatch_rejected",
                  "stdout": "", "stderr": "", "process_cleanup": proof, "cleanup_proven": proof["cleanup_proven"]}
    result.update(identity)
    result["runtime_digest"] = request["runtime_digest"]
    result["package_sha256"] = request["package_sha256"]
    result["input_sha256"] = request["input_sha256"]
    _write_private_output(output, "supervisor-result.json", json.dumps(result, sort_keys=True).encode())
    return 0 if result["cleanup_proven"] else 2


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
