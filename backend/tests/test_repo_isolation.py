"""#1065: real-process proof of repository command isolation.

Every isolated test runs actual commands under bubblewrap and skips cleanly
when this host cannot provide isolation (no bwrap, or user namespaces denied).
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import pytest

from src.execution.repo_isolation import (
    IsolationLimits, IsolationRuntime, IsolationUnavailable, StagePaths, clear_probe_cache, probe, wrap)

RUNTIME = IsolationRuntime.for_python()
SMALL = IsolationLimits(cpu_seconds=2, address_space_bytes=1024 ** 3, file_size_bytes=1024 ** 2,
                        max_processes=32, tmp_bytes=16 * 1024 ** 2)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake operator home and Seraph workspace; the stage lives inside it."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_test").write_text("PRIVATE KEY FIXTURE")
    workspace = home / "seraph-workspace"
    stage = workspace / "artifacts" / "repo-stage" / "job-1"
    (stage / "workspace").mkdir(parents=True)
    (stage / "input").mkdir()
    (stage / "input" / "patch.diff").write_text("--- a\n+++ b\n")
    (stage / "workspace" / "test_ok.py").write_text("print('snapshot readable')\n")
    (workspace / "seraph.db").write_text("SQLITE FIXTURE")
    (workspace / "vault").mkdir()
    (workspace / "vault" / "secret").write_text("VAULT FIXTURE")
    hidden = [home / ".ssh", workspace / "seraph.db", workspace / "vault"]
    posture = probe(RUNTIME, SMALL, hidden_paths=hidden, use_cache=False)
    if not posture.isolated:
        pytest.skip(f"host cannot isolate: {posture.reason}")
    paths = StagePaths(writable=(stage / "workspace",), read_only=(stage / "input",), cwd=stage / "workspace")
    return {"home": home, "workspace": workspace, "stage": stage, "paths": paths, "posture": posture}


def run(host, code, *, limits=SMALL, timeout=30):
    argv = wrap([str(RUNTIME.interpreter), "-I", "-S", "-c", code], host["paths"], RUNTIME, limits, host["posture"])
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                          start_new_session=True)


def test_network_is_refused_and_only_loopback_exists(host):
    result = run(host, """
import socket, errno
print(sorted(name for _, name in socket.if_nameindex()))
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect(("192.0.2.1", 80))  # TEST-NET-1: never routed even if reachable
    print("CONNECTED")
except OSError as exc:
    print("refused", errno.errorcode.get(exc.errno))
""")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["['lo']", "refused ENETUNREACH"]


def test_secrets_are_invisible_while_the_bound_stage_is_readable(host):
    stage, workspace, home = host["stage"], host["workspace"], host["home"]
    result = run(host, f"""
import os
for path in {[str(home / '.ssh' / 'id_test'), str(workspace / 'seraph.db'), str(workspace / 'vault' / 'secret'),
              os.path.expanduser('~/.ssh')]!r}:
    try:
        open(path).read(); print("READABLE", path)
    except OSError as exc:
        print("hidden", type(exc).__name__)
print(open({str(stage / 'workspace' / 'test_ok.py')!r}).read().strip())
print(open({str(stage / 'input' / 'patch.diff')!r}).read().splitlines()[0])
try:
    open({str(stage / 'input' / 'new')!r}, "w")
    print("INPUT WRITABLE")
except OSError as exc:
    print("input read-only", type(exc).__name__)
open("written.txt", "w").write("ok")
print(os.environ.get("HOME"), sorted(os.environ))
""")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[:4] == ["hidden FileNotFoundError"] * 4
    assert lines[4:7] == ["print('snapshot readable')", "--- a", "input read-only OSError"]
    # bwrap --chdir sets PWD; nothing from the operator environment leaks.
    assert lines[7] == "/tmp ['HOME', 'LANG', 'PATH', 'PWD', 'TMPDIR', 'TZ']"
    assert (stage / "workspace" / "written.txt").read_text() == "ok"
    assert not (stage / "input" / "new").exists()


def test_process_count_limit_stops_a_fork_loop(host):
    result = run(host, """
import os, time
children = 0
try:
    for _ in range(500):
        pid = os.fork()
        if pid == 0:
            time.sleep(5); os._exit(0)
        children += 1
except OSError as exc:
    print("fork denied after", children, exc.errno)
""")
    assert result.returncode == 0, result.stderr
    words = result.stdout.split()
    assert words[:3] == ["fork", "denied", "after"] and int(words[3]) < SMALL.max_processes


def test_file_size_and_address_space_limits_are_enforced(host):
    result = run(host, f"""
import errno
try:
    open("big.bin", "wb").write(b"x" * {SMALL.file_size_bytes * 2})
    print("FSIZE NOT ENFORCED")
except OSError as exc:
    print("fsize", errno.errorcode.get(exc.errno))
try:
    block = bytearray({SMALL.address_space_bytes * 2})
    print("AS NOT ENFORCED")
except MemoryError:
    print("as MemoryError")
""")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["fsize EFBIG", "as MemoryError"]
    assert (host["stage"] / "workspace" / "big.bin").stat().st_size <= SMALL.file_size_bytes


def test_cpu_limit_terminates_a_spinning_command(host):
    limits = SMALL
    started = time.monotonic()
    result = run(host, "while True:\n    pass\n", limits=limits, timeout=60)
    assert result.returncode != 0
    # bwrap reports a signalled child as 128 + signal number; the kernel sends
    # SIGXCPU at the soft limit and SIGKILL at the (equal) hard limit.
    assert result.returncode in {128 + signal.SIGXCPU, 128 + signal.SIGKILL}
    assert time.monotonic() - started < 30


def test_double_forked_detached_process_does_not_outlive_the_command(host):
    marker = "seraph-isolation-" + uuid.uuid4().hex
    result = run(host, f"""
import os, sys
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        os.execv("/usr/bin/sleep", ["sleep", "600", {marker!r}])
    os._exit(0)
print("parent exiting")
""")
    assert result.returncode == 0 and result.stdout.strip() == "parent exiting", result.stderr
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        survivors = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
        if not survivors:
            break
        time.sleep(0.1)
    assert survivors == []


def test_probe_reports_not_isolated_when_bwrap_is_missing(tmp_path, monkeypatch):
    clear_probe_cache()
    monkeypatch.setenv("PATH", str(tmp_path))  # no bwrap or prlimit on PATH
    posture = probe(RUNTIME, SMALL, use_cache=False)
    assert posture.variant == "not_isolated" and posture.reason == "bwrap_missing"
    assert posture.payload() == {
        "variant": "not_isolated", "isolation_claim": "none", "network_isolation": "not_isolated",
        "resource_enforcement": "admission_and_wall_timeout_only", "host_access": "operator_user_full_access",
        "limits_digest": SMALL.digest, "isolation_unavailable_reason": "bwrap_missing"}
    # No silent fallback: an unisolated posture can never produce a command.
    with pytest.raises(IsolationUnavailable):
        wrap(["true"], StagePaths(), RUNTIME, SMALL, posture)


def test_probe_reports_not_isolated_when_user_namespaces_are_denied(tmp_path, monkeypatch):
    clear_probe_cache()
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\necho 'bwrap: setting up uid map: Operation not permitted' >&2\nexit 1\n")
    fake.chmod(0o755)
    prlimit = Path("/usr/bin/prlimit")
    if not prlimit.exists():
        pytest.skip("prlimit unavailable")
    (tmp_path / "prlimit").symlink_to(prlimit)
    monkeypatch.setenv("PATH", str(tmp_path))
    posture = probe(RUNTIME, SMALL, use_cache=False)
    assert posture.variant == "not_isolated" and posture.reason == "userns_denied"


def test_probe_reports_mismatch_when_a_hidden_path_would_be_visible(tmp_path):
    # The runtime prefix is bound read-only, so claiming it hidden must fail.
    posture = probe(RUNTIME, SMALL, hidden_paths=[sys.base_prefix], use_cache=False)
    if posture.reason in {"bwrap_missing", "userns_denied", "prlimit_missing"}:
        pytest.skip(f"host cannot isolate: {posture.reason}")
    assert posture.variant == "not_isolated" and posture.reason == "probe_mismatch"


def test_wrap_rejects_changed_limits_and_unsafe_inputs(host):
    with pytest.raises(IsolationUnavailable):
        wrap(["true"], host["paths"], RUNTIME, IsolationLimits(), host["posture"])
    for bad_env in ({"BAD-NAME": "x"}, {"OK": "nul\x00"}):
        with pytest.raises(ValueError):
            wrap(["true"], host["paths"], RUNTIME, SMALL, host["posture"], env=bad_env)
    with pytest.raises(ValueError):
        wrap(["true"], StagePaths(writable=(Path("/usr/lib"),)), RUNTIME, SMALL, host["posture"])
    with pytest.raises(ValueError):
        wrap(["true"], StagePaths(writable=(Path("relative"),)), RUNTIME, SMALL, host["posture"])
    with pytest.raises(ValueError):
        wrap([], host["paths"], RUNTIME, SMALL, host["posture"])
