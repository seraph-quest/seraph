"""Prepare three genuine Node profile inputs in a private CI-owned prefix.

The frozen profile executes Node directly; npm is identity metadata, not an
executed package manager. Source cache permissions are never changed.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

FILES = {"bin/node": 256 * 1024 * 1024,
         "lib/node_modules/npm/package.json": 2 * 1024 * 1024,
         "lib/node_modules/npm/bin/npm-cli.js": 2 * 1024 * 1024}


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextlib.contextmanager
def parent(path):
    require(path.is_absolute() and ".." not in path.parts, "canonical absolute path required")
    held = []
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        held.append((fd, None, None, os.fstat(fd)))
        for part in path.parent.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            held.append((child, fd, part, os.fstat(child)))
            fd = child
        yield fd
        for opened, ancestor, name, original in held:
            current = os.fstat(opened)
            require((current.st_dev, current.st_ino, current.st_uid, current.st_mode) ==
                    (original.st_dev, original.st_ino, original.st_uid, original.st_mode), "directory identity changed")
            if ancestor is not None:
                named = os.stat(name, dir_fd=ancestor, follow_symlinks=False)
                require((named.st_dev, named.st_ino, named.st_uid, named.st_mode) ==
                        (current.st_dev, current.st_ino, current.st_uid, current.st_mode), "directory entry changed")
    finally:
        for fd, _, _, _ in reversed(held):
            os.close(fd)


def read_source(path, limit):
    with parent(path) as directory:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            require(stat.S_ISREG(before.st_mode) and before.st_size <= limit, "source type or byte bound invalid")
            require(identity(before) == identity(os.stat(path.name, dir_fd=directory, follow_symlinks=False)), "source entry changed")
            chunks = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            require(len(data) == before.st_size and len(data) <= limit, "source bytes changed or exceeded bound")
            require(identity(before) == identity(os.fstat(fd)) == identity(os.stat(path.name, dir_fd=directory, follow_symlinks=False)), "source identity changed during read")
            return identity(before), hashlib.sha256(data).hexdigest(), data
        finally:
            os.close(fd)


def copy_prefix(source, destination):
    """Copy the closed three files; return paths and source/owned byte proof."""
    records = {name: read_source(source / name, limit) for name, limit in FILES.items()}
    with parent(destination) as directory:
        os.mkdir(destination.name, 0o700, dir_fd=directory)
    root = destination.lstat()
    require(stat.S_ISDIR(root.st_mode) and root.st_uid == os.geteuid() and root.st_mode & 0o777 == 0o700, "private destination custody invalid")
    result = {}
    def owned_directory(folder):
        folder.mkdir(mode=0o700, exist_ok=True)
        with parent(folder) as directory:
            fd = os.open(folder.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                opened = os.fstat(fd)
                require(opened.st_uid == os.geteuid() and identity(opened) == identity(os.stat(folder.name, dir_fd=directory, follow_symlinks=False)), "owned directory custody invalid")
                os.fchmod(fd, 0o755)
                require(identity(os.fstat(fd)) == identity(os.stat(folder.name, dir_fd=directory, follow_symlinks=False)), "owned directory changed")
            finally:
                os.close(fd)
    for name, (source_identity, digest, data) in records.items():
        target = destination / name
        for relative in reversed(target.parent.relative_to(destination).parents):
            folder = destination / relative
            if folder != destination:
                owned_directory(folder)
        owned_directory(target.parent)
        with parent(target) as directory:
            fd = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                opened = os.fstat(fd)
                require(opened.st_uid == os.geteuid() and opened.st_nlink == 1 and stat.S_ISREG(opened.st_mode), "owned copy custody invalid")
                require(identity(opened) == identity(os.stat(target.name, dir_fd=directory, follow_symlinks=False)), "owned entry changed")
                with os.fdopen(os.dup(fd), "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.fchmod(fd, 0o755 if name == "bin/node" else 0o644)
                owned = os.fstat(fd)
                require(identity(owned) == identity(os.stat(target.name, dir_fd=directory, follow_symlinks=False)), "owned copy changed")
            finally:
                os.close(fd)
        _, copied_digest, _ = read_source(target, FILES[name])
        require(copied_digest == digest and owned.st_uid == os.geteuid() and owned.st_nlink == 1 and not owned.st_mode & 0o022, "owned copy byte or mode mismatch")
        result[name] = {"sha256": digest, "source_identity": source_identity, "owned_identity": identity(owned)}
    for name, limit in FILES.items():
        current_identity, digest, _ = read_source(source / name, limit)
        require((current_identity, digest) == records[name][:2], "source changed across preparation")
    require((destination.lstat().st_dev, destination.lstat().st_ino) == (root.st_dev, root.st_ino), "private root changed")
    return {"node_path": str(destination / "bin/node"), "files": result, "copied_files": 3}


def regular(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not info.st_mode & 0o022, "native dependency must be a trusted regular file")


def main():
    selected = Path(shutil.which("node") or "")
    require(selected.is_absolute(), "selected Node executable missing")
    original = selected.lstat()
    print(json.dumps({"original_node": str(selected), "mode": oct(original.st_mode), "uid": original.st_uid, "nlink": original.st_nlink, "size": original.st_size}), flush=True)
    source = Path(os.environ["RUNNER_TOOL_CACHE"]) / "node/24.21.0/x64"
    require(selected == source / "bin/node", "Node selection differs from exact setup-node distribution")
    receipt = copy_prefix(source, Path(os.environ["RUNNER_TEMP"]) / "seraph-native-node24")
    node = Path(receipt["node_path"])
    regular(node)
    require(os.access(node, os.X_OK), "selected Node not executable")
    version = subprocess.run([str(node), "--version"], check=True, capture_output=True, timeout=2, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}).stdout.decode().strip()
    require(version == "v24.21.0" and hashlib.sha256(node.read_bytes()).hexdigest() == receipt["files"]["bin/node"]["sha256"], "Node version or bytes changed")
    typescript = Path(os.environ["GITHUB_WORKSPACE"]) / "frontend/node_modules/typescript"
    require(typescript.is_dir() and not typescript.is_symlink(), "TypeScript directory missing or linked")
    regular(typescript / "package.json")
    actual = json.loads((typescript / "package.json").read_text())["version"]
    lock = typescript.parents[1] / "package-lock.json"
    require(actual == json.loads(lock.read_text())["packages"]["node_modules/typescript"]["version"], "TypeScript differs from checked-in lock")
    require(all("\n" not in str(path) and "\r" not in str(path) for path in (node, typescript)), "invalid environment path")
    with open(os.environ["GITHUB_ENV"], "a") as output:
        output.write(f"SERAPH_TEST_NODE_RUNTIME={node}\nSERAPH_TEST_TYPESCRIPT_ROOT={typescript}\n")
    print(json.dumps({"owned_node_readiness": receipt, "version": version, "typescript": actual}))


if __name__ == "__main__":
    main()
