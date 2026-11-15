"""Actual fixed child denial before any private input; no network destination."""
import errno
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.work_board import document_read_child as child


def run_child_probe(script):
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={}, timeout=10, check=False)
    assert result.returncode == 0, result.stderr.decode()
    assert len(result.stdout) <= 4096
    return result.stdout.decode()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux seccomp syscall proof")
def test_actual_child_all_io_uring_syscalls_and_socket_families_are_eperm():
    script = f"""
import ctypes, errno, json, runpy, socket
module = runpy.run_path({str(Path(child.__file__).resolve())!r})
library = ctypes.CDLL('libseccomp.so.2')
library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
numbers = {{name: library.seccomp_syscall_resolve_name(name.encode()) for name in module['_IO_URING_SYSCALLS']}}
assert all(number >= 0 for number in numbers.values())
module['confine']()
module['_probe_io_uring_denial'](numbers)
for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
    try:
        handle = socket.socket(family)
    except OSError as exc:
        assert exc.errno == errno.EPERM
    else:
        handle.close()
        raise AssertionError('socket was admitted')
print(json.dumps({{'io_uring_syscalls': sorted(numbers), 'errno': errno.EPERM, 'private_input_read': False}}))
"""
    proof = json.loads(run_child_probe(script))
    assert proof == {"io_uring_syscalls": sorted(child._IO_URING_SYSCALLS),
        "errno": errno.EPERM, "private_input_read": False}


@pytest.mark.skipif(sys.platform != "linux", reason="Linux mandatory syscall resolution")
@pytest.mark.parametrize("missing", child._IO_URING_SYSCALLS)
def test_unresolved_mandatory_name_blocks_before_ready_or_private_input(missing):
    script = f"""
import ctypes, runpy, sys
module = runpy.run_path({str(Path(child.__file__).resolve())!r})
original_cdll = ctypes.CDLL
def cdll(name, *args, **kwargs):
    library = original_cdll(name, *args, **kwargs)
    if name == 'libseccomp.so.2':
        resolve = library.seccomp_syscall_resolve_name
        resolve.argtypes = [ctypes.c_char_p]
        def resolve_name(value):
            return -1 if value == {missing.encode()!r} else resolve(value)
        library.seccomp_syscall_resolve_name = resolve_name
    return library
ctypes.CDLL = cdll
class NoPrivateInput:
    @property
    def buffer(self):
        raise AssertionError('private input read before confinement')
sys.stdin = NoPrivateInput()
assert module['main']() == 0
"""
    output = run_child_probe(script)
    assert '"state": "ready"' not in output
    assert json.loads(output)["reason"] == "document_confinement_unavailable"


@pytest.mark.parametrize("result,error", [(-1, errno.ENOSYS), (-1, errno.EINVAL),
    (-1, errno.EACCES), (0, 0)])
def test_readiness_probe_requires_actual_eperm(monkeypatch, result, error):
    class Syscall:
        def __call__(self, *_args):
            child.ctypes.set_errno(error)
            return result
    class Libc:
        syscall = Syscall()
    monkeypatch.setattr(child.ctypes, "CDLL", lambda *_args, **_kwargs: Libc())
    with pytest.raises(RuntimeError, match="io_uring confinement ineffective"):
        child._probe_io_uring_denial(dict.fromkeys(child._IO_URING_SYSCALLS, 1))
