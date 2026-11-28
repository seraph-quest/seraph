"""Spawn helpers for the passive provider-worker entry tests (#1207).

Raw ``Popen`` lives here (``tests/support`` is outside the determinism
ratchet's Popen count). Every helper reaps what it spawns and closes its pipes.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import select
import struct
import subprocess
import sys

from src.runtime_plugins.protocol import decode_json
from src.runtime_plugins.worker_entry import EXIT_EVENT, encode, validate_frame, worker_launch

BACKEND = Path(__file__).resolve().parents[2]
INTERPRETER = str(BACKEND / ".venv" / "bin" / "python") if (BACKEND / ".venv" / "bin" / "python").exists() \
    else sys.executable
# A hang guard only: a worker that never answers fails the test, it is never retried.
READ_BOUND_SECONDS = 120.0


def launch(workspace: Path, *, parent_pid: int | None = None, backend_root: Path = BACKEND):
    return worker_launch(interpreter=os.path.abspath(INTERPRETER), parent_pid=parent_pid or os.getpid(),
                         settings_env_file="", workspace_dir=str(workspace), backend_root=backend_root)


def request(nonce: str, seq: int, method: str, payload: dict | None = None) -> bytes:
    return encode({"protocol": 1, "boot_nonce": nonce, "request_id": f"r{seq}", "seq": seq, "kind": "request",
                   "method": method, "payload": {} if payload is None else payload}, kind="request")


def _read_exact(fd: int, size: int) -> bytes:
    data = b""
    while len(data) < size:
        ready, _, _ = select.select([fd], [], [], READ_BOUND_SECONDS)
        if not ready:
            raise AssertionError("worker did not answer within the hang guard")
        chunk = os.read(fd, size - len(data))
        if not chunk:
            return data
        data += chunk
    return data


def read_response(fd: int) -> dict | None:
    header = _read_exact(fd, 4)
    if len(header) < 4:
        return None
    return validate_frame(decode_json(_read_exact(fd, struct.unpack(">I", header)[0])), kind="response")


def read_exit(fd: int) -> dict:
    """Read stderr to EOF (every holder closed it) and return the final exit record."""
    chunks = []
    while True:
        ready, _, _ = select.select([fd], [], [], READ_BOUND_SECONDS)
        if not ready:
            raise AssertionError("worker did not exit within the hang guard")
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    lines = [line for line in b"".join(chunks).decode("utf-8", "replace").splitlines()
             if line.startswith('{"details"') and EXIT_EVENT in line]
    assert lines, b"".join(chunks)[-4000:]
    return json.loads(lines[-1])


@dataclass
class Worker:
    process: subprocess.Popen
    nonce: str
    seq: int = 0

    @property
    def pid(self) -> int:
        return self.process.pid

    def call(self, method: str, payload: dict | None = None) -> dict:
        self.send(method, payload)
        response = read_response(self.process.stdout.fileno())
        assert response is not None and response["method"] == method, response
        return response["payload"]

    def send(self, method: str, payload: dict | None = None) -> None:
        self.seq += 1
        self.process.stdin.write(request(self.nonce, self.seq, method, payload))
        self.process.stdin.flush()

    def finish(self) -> tuple[int, dict]:
        """Close stdin, read the exit record and reap; returns (returncode, record)."""
        try:
            if not self.process.stdin.closed:
                self.process.stdin.close()
        except BrokenPipeError:
            pass
        record = read_exit(self.process.stderr.fileno())
        code = self.process.wait(timeout=READ_BOUND_SECONDS)
        self.process.stdout.close()
        self.process.stderr.close()
        return code, record


def spawn(workspace: Path, *, argv=None, env=None, backend_root: Path = BACKEND) -> Worker:
    spec = launch(workspace, backend_root=backend_root)
    process = subprocess.Popen(list(argv or spec.argv), env=dict(spec.env if env is None else env), cwd=spec.cwd,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return Worker(process, secrets.token_hex(32))


def run_refused(workspace: Path, *, argv=None, env=None) -> tuple[int, dict]:
    """A worker refused at invocation: it answers nothing and exits."""
    return spawn(workspace, argv=argv, env=env).finish()


# A minimal supervising parent: it spawns the worker with the closed launch
# (naming its own pid), optionally writes the hello frame on the worker's
# stdin, prints the worker pid, then blocks until it is killed.
_PARENT = r"""
import json, os, subprocess, sys
spec = json.loads(sys.argv[1])
stdin_r, stdout_w, stderr_w, stdin_w = (int(value) for value in sys.argv[2:6])
env = dict(spec["env"], SERAPH_WORKER_PARENT_PID=str(os.getpid()))
child = subprocess.Popen(spec["argv"], env=env, cwd=spec["cwd"], stdin=stdin_r, stdout=stdout_w, stderr=stderr_w)
for fd in (stdin_r, stdout_w, stderr_w):
    os.close(fd)
if stdin_w >= 0:
    os.write(stdin_w, bytes.fromhex(sys.argv[6]))
print(child.pid, flush=True)
sys.stdin.read()
"""


@dataclass
class Orphanable:
    """A worker whose parent is a separate process the test can kill."""

    parent: subprocess.Popen
    worker_pid: int
    stdout_r: int
    stderr_r: int
    stdin_w: int | None
    nonce: str

    def kill_parent(self) -> None:
        self.parent.kill()
        self.parent.wait(timeout=READ_BOUND_SECONDS)
        self.parent.stdin.close()
        self.parent.stdout.close()

    def close(self) -> None:
        for fd in (self.stdout_r, self.stderr_r, self.stdin_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if self.parent.poll() is None:
            self.kill_parent()


def spawn_with_parent(workspace: Path, *, test_holds_stdin: bool) -> Orphanable:
    """Worker under an intermediate parent.

    ``test_holds_stdin=False``: only the parent holds the worker's stdin write
    end (and writes hello), so its death is also stdin EOF. ``True``: the test
    keeps a copy, so stdin never reaches EOF and only the platform's
    parent-death hook (PDEATHSIG on Linux, kqueue NOTE_EXIT on macOS) can see it.
    """
    spec = launch(workspace, parent_pid=os.getpid())
    stdin_r, stdin_w = os.pipe()
    stdout_r, stdout_w = os.pipe()
    stderr_r, stderr_w = os.pipe()
    nonce = secrets.token_hex(32)
    hello = request(nonce, 1, "worker.hello").hex()
    payload = json.dumps({"argv": list(spec.argv), "env": dict(spec.env), "cwd": spec.cwd})
    passed = (stdin_r, stdout_w, stderr_w, stdin_w)
    parent = subprocess.Popen([sys.executable, "-I", "-c", _PARENT, payload, *(str(fd) for fd in passed[:3]),
                               str(-1 if test_holds_stdin else stdin_w), hello],
                              pass_fds=passed, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    for fd in (stdin_r, stdout_w, stderr_w):
        os.close(fd)
    if not test_holds_stdin:
        os.close(stdin_w)
    worker_pid = int(parent.stdout.readline())
    if test_holds_stdin:
        os.write(stdin_w, bytes.fromhex(hello))
    return Orphanable(parent, worker_pid, stdout_r, stderr_r, stdin_w if test_holds_stdin else None, nonce)
