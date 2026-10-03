"""One optional fixed Linux JSON profile; never a general executable selector."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import sys

PROFILE = "json-python-bwrap-v1"
PACKAGE_ID = "seraph.tool.json-format"
CAPABILITY = "work.json-format.v1"
JOB_KIND = "local_json_format"
BWRAP_SHA256 = "95a4c13e9652537a941aea7c714516f199f477312eadd4f172845e0e0b4f87f5"
BWRAP_SOURCE = "2a76602a8c71f36c1527cf9fc3417d9149822e0c"
EMBEDDING_SHA256 = "409611f20b3c59146155e6dad7e274e062bc9b756085c81d479bd6365da8fb54"
EMBEDDING_SOURCE_SHA256 = "79d92a7f7888116c0e3bc6ad1358419877b82e59127ed0d35091bc75c8d65d69"
MAX_INPUT = 32768
MAX_OUTPUT = 65536
MAX_STREAM = 8192
MAX_SECONDS = 10
STDLIB_FILES = (
    "_collections_abc.py", "abc.py", "codecs.py", "collections/__init__.py",
    "copyreg.py", "ctypes/__init__.py", "ctypes/_endian.py", "encodings/__init__.py",
    "encodings/aliases.py", "encodings/utf_8.py", "enum.py", "functools.py",
    "genericpath.py", "importlib/_bootstrap_external.py", "io.py", "json/__init__.py",
    "json/decoder.py", "json/encoder.py", "json/scanner.py", "keyword.py", "operator.py",
    "os.py", "posixpath.py", "re/__init__.py", "re/_casefix.py", "re/_compiler.py",
    "re/_constants.py", "re/_parser.py", "reprlib.py", "stat.py", "struct.py",
    "types.py", "zipimport.py",
)
SYSTEM_LIBRARIES = ("libpthread.so.0", "libdl.so.2", "libutil.so.1", "libm.so.6",
                    "librt.so.1", "libc.so.6")


class ToolPackageBlocked(ValueError):
    pass


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def source_package() -> Path:
    return Path(__file__).resolve().parents[1] / "defaults/tool-packages/json-format-v1/formatter.py"


def package_manifest():
    from src.extensions.capability_pack import parse_capability_pack_manifest
    path = source_package().parent / "manifest.yaml"
    return parse_capability_pack_manifest(path.read_text(encoding="utf-8"), source=str(path))


def bootstrap() -> Path:
    return Path(__file__).with_name("tool_package_bootstrap.py")


def native_platform() -> None:
    if sys.platform != "linux" or platform.machine() != "x86_64" or sys.maxsize != 2**63-1:
        raise ToolPackageBlocked("tool_package_profile_unsupported_platform")
    if sys.version_info[:3] != (3, 12, 8):
        raise ToolPackageBlocked("tool_package_profile_interpreter_unavailable")


def expected_runtime_files() -> dict[str, Path]:
    """Fixed reviewed closure of the running trusted CPython distribution.

    This is metadata only, not an installer. A different dynamic-extension or
    platform closure remains Blocked until explicitly reviewed for this profile.
    """
    native_platform()
    prefix = Path(sys.base_prefix)
    stdlib = prefix / "lib/python3.12"
    result = {"runtime/bin/isolated-python": Path(__file__).resolve().parents[3] / "build/916-embedding-r5/isolated-python",
              "runtime/lib/libpython3.12.so.1.0": prefix / "lib/libpython3.12.so.1.0",
              "lib64/ld-linux-x86-64.so.2": Path("/lib64/ld-linux-x86-64.so.2").resolve(),
              "bootstrap.py": bootstrap(), "launcher.c": Path(__file__).with_name("tool_package_launcher.c")}
    result.update({"runtime/lib/python3.12/"+name: stdlib/name for name in STDLIB_FILES})
    result.update({"lib/x86_64-linux-gnu/"+name: Path("/lib/x86_64-linux-gnu")/name
                   for name in SYSTEM_LIBRARIES})
    return result


def read_private(root: Path, reference: str, maximum: int) -> bytes:
    from src.execution.repo_sandbox import _open_trusted_directory
    parts = PurePosixPath(reference).parts
    if not parts or PurePosixPath(reference).is_absolute() or ".." in parts:
        raise ToolPackageBlocked("tool_package_reference_invalid")
    directory = _open_trusted_directory(root)
    fd = -1
    try:
        for name in parts[:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
            meta = os.fstat(directory)
            if meta.st_uid != os.getuid() or meta.st_mode & 0o077:
                raise ToolPackageBlocked("tool_package_directory_untrusted")
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077 or meta.st_nlink != 1 or meta.st_size > maximum:
            raise ToolPackageBlocked("tool_package_file_untrusted")
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > maximum:
            raise ToolPackageBlocked("tool_package_file_limit")
        final = os.fstat(fd)
        if (meta.st_ino, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns) != (final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns):
            raise ToolPackageBlocked("tool_package_file_changed")
        return bytes(raw)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(directory)


def inspect_runtime(root: Path) -> dict:
    native_platform()
    descriptor = json.loads(read_private(root, "profile.json", 16384))
    expected = expected_runtime_files()
    if (not isinstance(descriptor, dict) or set(descriptor) != {"schema", "profile", "source_commit", "assume_kernel", "files"}
        or descriptor["schema"] != 1 or descriptor["profile"] != PROFILE
        or descriptor["source_commit"] != BWRAP_SOURCE or descriptor["assume_kernel"] != ""
        or not isinstance(descriptor["files"], dict)
        or set(descriptor["files"]) != {"bwrap", *("rootfs/"+name for name in expected)}):
        raise ToolPackageBlocked("tool_package_runtime_binding_invalid")
    for name, origin in expected.items():
        raw = read_private(root, "rootfs/"+name, 64*1024*1024)
        # No caller-selected executable, bootstrap, Python module or library.
        if digest(raw) != digest(origin.read_bytes()) or descriptor["files"]["rootfs/"+name] != digest(raw):
            raise ToolPackageBlocked("tool_package_runtime_digest_changed")
        if name == "runtime/bin/isolated-python" and digest(raw) != EMBEDDING_SHA256:
            raise ToolPackageBlocked("tool_package_embedding_digest_changed")
        if name == "launcher.c" and digest(raw) != EMBEDDING_SOURCE_SHA256:
            raise ToolPackageBlocked("tool_package_embedding_source_changed")
    if digest(read_private(root, "bwrap", 1024*1024)) != BWRAP_SHA256 or descriptor["files"]["bwrap"] != BWRAP_SHA256:
        raise ToolPackageBlocked("tool_package_launcher_digest_changed")
    # Unlisted files, links and writable placeholder content cannot enter rootfs.
    actual = set()
    placeholders = {"rootfs/input.json", "rootfs/package.py", "rootfs/out/result.json"}
    for path in (root/"rootfs").rglob("*"):
        meta = path.lstat()
        if stat.S_ISLNK(meta.st_mode):
            raise ToolPackageBlocked("tool_package_runtime_symlink")
        if stat.S_ISREG(meta.st_mode):
            relative = path.relative_to(root).as_posix()
            actual.add(relative)
            if relative in placeholders and read_private(root, relative, 1) != b"":
                raise ToolPackageBlocked("tool_package_placeholder_changed")
        elif not stat.S_ISDIR(meta.st_mode):
            raise ToolPackageBlocked("tool_package_runtime_special_file")
    if actual != set(descriptor["files"])-{"bwrap"} | placeholders:
        raise ToolPackageBlocked("tool_package_runtime_extra_file")
    return {"profile": PROFILE, "runtime_digest": digest(canonical(descriptor)),
            "launcher_sha256": BWRAP_SHA256, "bootstrap_sha256": digest(bootstrap().read_bytes()),
            "embedding_sha256": EMBEDDING_SHA256, "embedding_source_sha256": EMBEDDING_SOURCE_SHA256,
            "supervisor_sha256": digest(Path(__file__).with_name("tool_package_supervisor.py").read_bytes()),
            "controller_sha256": digest(Path(__file__).with_name("tool_package_runner.py").read_bytes()),
            "package_sha256": digest(source_package().read_bytes()), "architecture": "native-linux-x86_64",
            "network": False, "secrets": [], "no_learning": True,
            "limits": {"cpu_seconds": 2, "address_space_bytes": 128*1024*1024,
                "max_seconds": MAX_SECONDS, "max_processes": 1, "max_fds": 64,
                "max_input_bytes": MAX_INPUT, "max_output_bytes": MAX_OUTPUT,
                "max_stream_bytes": MAX_STREAM, "max_attempts": 1}}


def expected_output(raw: bytes) -> bytes:
    if len(raw) > MAX_INPUT:
        raise ToolPackageBlocked("tool_package_input_limit")
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result
    def nonfinite(value):
        raise ValueError("non-finite JSON number")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite)
        pending = [(value, 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if count > 4096 or depth > 32:
                raise ValueError("JSON structure limit")
            if isinstance(item, dict):
                pending.extend((child, depth+1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth+1) for child in item)
        output = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+"\n").encode()
        if len(output) > MAX_OUTPUT:
            raise ValueError("formatted JSON limit")
        return output
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ToolPackageBlocked("tool_package_json_invalid") from exc
