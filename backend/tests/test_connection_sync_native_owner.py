"""Fixed Darwin ABI fixtures; these do not claim native macOS execution."""
import ctypes
import pytest
from src.integrations import native_physical_owner as native

BOOT = "12345678-1234-4234-8234-123456789abc"


def test_darwin_native_abi_and_fixed_library_calls(monkeypatch):
    calls = []
    class Query:
        def __init__(self, operation):
            self.operation = operation
        def __call__(self, *args):
            calls.append(args)
            return self.operation(*args)
    def boot(name, buffer, size, new, length):
        assert name == b"kern.bootsessionuuid" and new is None and length == 0
        ctypes.memmove(buffer, BOOT.encode() + b"\0", 37)
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = 37
        return 0
    def start(pid, flavor, arg, buffer, size):
        assert pid == 23 and flavor == 3 and arg == 0 and size == 136
        info = ctypes.cast(buffer, ctypes.POINTER(native._DarwinBsdInfo)).contents
        info.pid, info.start_sec, info.start_usec = pid, 1700000000, 42
        return 136
    class Library:
        sysctlbyname = Query(boot)
        proc_pidinfo = Query(start)
    def load(path, **kwargs):
        assert path in {"/usr/lib/libSystem.B.dylib", "/usr/lib/libproc.dylib"}
        assert kwargs == {"use_errno": True}
        return Library()
    monkeypatch.setattr(native.ctypes, "CDLL", load)
    assert native._darwin_boot_id() == BOOT
    assert native._darwin_start(23) == (1700000000, 42)
    assert ctypes.sizeof(native._DarwinBsdInfo) == 136
    assert len(calls) == 2


def test_darwin_missing_pid_and_cross_platform_remain_unknown(monkeypatch):
    monkeypatch.setattr(native, "_host_platform", lambda: "darwin")
    monkeypatch.setattr(native, "_darwin_boot_id", lambda: BOOT)
    witness = {"platform": "darwin", "boot_id": BOOT, "pid": 23,
               "pid_start_sec": 1700000000, "pid_start_usec": 42}
    def unavailable(pid):
        raise OSError("missing PID is unknown")
    monkeypatch.setattr(native, "_darwin_start", unavailable)
    assert native.positive_owner_death(witness) is None
    monkeypatch.setattr(native, "_darwin_start", lambda pid: (1700000001, 0))
    assert native.positive_owner_death(witness) == "positive_process_death"
    monkeypatch.setattr(native, "_darwin_boot_id", lambda: "87654321-4321-4321-8321-cba987654321")
    assert native.positive_owner_death(witness) == "darwin_boot_changed"
    assert native.positive_owner_death({**witness, "platform": "linux"}) is None


@pytest.mark.parametrize("size,pid,seconds,microseconds", [(0,23,1,0),(135,23,1,0),(136,24,1,0),(136,23,0,0),(136,23,1,1000000)])
def test_darwin_short_wrong_or_invalid_native_result_fails_closed(monkeypatch, size, pid, seconds, microseconds):
    class Query:
        def __call__(self, requested, flavor, arg, buffer, length):
            info = ctypes.cast(buffer, ctypes.POINTER(native._DarwinBsdInfo)).contents
            info.pid, info.start_sec, info.start_usec = pid, seconds, microseconds
            return size
    class Library:
        proc_pidinfo = Query()
    monkeypatch.setattr(native.ctypes, "CDLL", lambda *args, **kwargs: Library())
    with pytest.raises(OSError):
        native._darwin_start(23)
