"""Bounded local Git producer. No credentials, network, operator-tree writes or hooks.

V1 accepts a complete bounded regular-file repository and loose SHA-1 objects.
Packed/alternate/linked object stores are explicitly unavailable, not decoded by
an operator-configured Git process. Production publication uses GitHub REST.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time
import zlib

from src.execution.repo_worker import _open_source_regular_file, _walk_tree

MAX_BYTES = 64 * 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
OID = re.compile(r"^[0-9a-f]{40}$")
PROFILE = "repo-publication-local-git-v1"


class PublicationError(ValueError):
    def __init__(self, code: str, *, status_code: int = 409):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_manifest(root: Path) -> list[dict]:
    result = []
    for relative, _, directory, metadata in _walk_tree(root):
        if directory or relative == ".git" or relative.startswith(".git/"):
            continue
        fd, initial = _open_source_regular_file(root, relative, expected_stat=metadata)
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
            final = os.fstat(handle.fileno())
        if len(raw) > 2 * 1024 * 1024 or (initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns):
            raise PublicationError("source_changed")
        result.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "mode": "100755" if metadata.st_mode & 0o111 else "100644"})
    return sorted(result, key=lambda item: item["path"])


def branch(value: str, *, feature=False) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,119}", value) or any(part in {"", ".", ".."} for part in value.split("/")) or ".." in value or value.endswith((".lock", ".", "/")):
        raise PublicationError("branch_invalid", status_code=422)
    if feature and not value.startswith(("feat/", "fix/")):
        raise PublicationError("feature_branch_required", status_code=422)
    return value


def oid(value: str) -> str:
    if not isinstance(value, str) or not OID.fullmatch(value):
        raise PublicationError("git_identity_invalid", status_code=422)
    return value


def object_id(kind: str, raw: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(raw)}\0".encode() + raw).hexdigest()


class SourceGit:
    """Read objects without invoking Git or importing source config/filters."""
    def __init__(self, root: Path):
        self.root = root
        self.control = root / ".git"
        if not self.control.is_dir() or self.control.is_symlink():
            raise PublicationError("linked_git_repository_unsupported")
        if (self.control / "objects/info/alternates").exists() or (self.control / "shallow").exists():
            raise PublicationError("alternate_or_shallow_repository_unsupported")
        self.objects: dict[str, tuple[str, bytes]] = {}
        self.bytes = 0

    def read_file(self, relative: str, maximum=MAX_OUTPUT) -> bytes:
        try:
            fd, initial = _open_source_regular_file(self.control, relative)
            with os.fdopen(fd, "rb") as handle:
                raw = handle.read(maximum + 1)
                final = os.fstat(handle.fileno())
            if len(raw) > maximum or initial.st_mtime_ns != final.st_mtime_ns or initial.st_size != final.st_size:
                raise PublicationError("git_control_changed")
            return raw
        except (OSError, ValueError) as exc:
            raise PublicationError("git_object_or_control_unavailable") from exc

    def head(self) -> str:
        value = self.read_file("HEAD", 1024).decode("ascii").strip()
        if value.startswith("ref: refs/heads/"):
            name = branch(value[len("ref: refs/heads/"):])
            value = self.read_file(f"refs/heads/{name}", 1024).decode("ascii").strip()
        return oid(value)

    def object(self, identity: str, expected: str | None = None) -> bytes:
        oid(identity)
        if identity not in self.objects:
            packed = self.read_file(f"objects/{identity[:2]}/{identity[2:]}", MAX_OUTPUT)
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(packed, MAX_OUTPUT + 1)
            if len(decoded) > MAX_OUTPUT or not decoder.eof or decoder.unused_data:
                raise PublicationError("git_object_invalid")
            header, raw = decoded.split(b"\0", 1)
            kind, size = header.decode("ascii").split(" ")
            if kind not in {"commit", "tree", "blob"} or int(size) != len(raw) or object_id(kind, raw) != identity:
                raise PublicationError("git_object_invalid")
            self.bytes += len(raw)
            if self.bytes > MAX_BYTES or len(self.objects) >= 4000:
                raise PublicationError("git_capture_limit")
            self.objects[identity] = (kind, raw)
        kind, raw = self.objects[identity]
        if expected and kind != expected:
            raise PublicationError("git_object_kind_invalid")
        return raw

    def tree(self, commit: str) -> tuple[str, list[dict], dict[str, bytes]]:
        raw = self.object(commit, "commit")
        first = raw.split(b"\n", 1)[0].decode("ascii")
        if not first.startswith("tree "):
            raise PublicationError("git_commit_invalid")
        tree_id = oid(first[5:])
        files, contents = [], {}

        def visit(identity, prefix="", depth=0):
            if depth > 16:
                raise PublicationError("git_capture_limit")
            value = self.object(identity, "tree")
            cursor = 0
            while cursor < len(value):
                end = value.index(b"\0", cursor)
                mode, name = value[cursor:end].split(b" ", 1)
                name = name.decode("utf-8")
                if name in {"", ".", "..", ".git"} or "/" in name or "\\" in name:
                    raise PublicationError("git_path_invalid")
                entry_id = value[end + 1:end + 21].hex()
                cursor = end + 21
                path = prefix + name
                if mode == b"40000":
                    visit(entry_id, path + "/", depth + 1)
                elif mode in {b"100644", b"100755"}:
                    content = self.object(entry_id, "blob")
                    if path in contents or len(files) >= 2000:
                        raise PublicationError("git_capture_limit")
                    contents[path] = content
                    files.append({"path": path, "mode": mode.decode(), "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
                else:
                    raise PublicationError("git_mode_unsupported")
        visit(tree_id)
        return tree_id, sorted(files, key=lambda item: item["path"]), contents


def equivalent(actual, tested) -> None:
    if not isinstance(tested, list) or not tested or actual != tested:
        raise PublicationError("tested_base_equivalence_mismatch")


def posture() -> dict:
    executable = Path("/usr/bin/git")
    if not executable.is_file():
        raise PublicationError("local_git_unavailable")
    return {"profile": PROFILE, "executor_kind": "local", "isolation_claim": "none", "git_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(), "network": "disabled", "hooks": "disabled", "filters": "disabled", "max_seconds": 30, "max_output_bytes": MAX_OUTPUT}


def _bounded_git(command, *, stage, env, data, deadline):
    """Bound combined output while reading it, including during stdin writes."""
    if time.monotonic() >= deadline:
        raise PublicationError("local_git_deadline")
    process = subprocess.Popen(command, cwd=stage, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    output, total, cursor = bytearray(), 0, 0
    data = data or b""
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            if data:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PublicationError("local_git_deadline")
                for key, _ in selector.select(min(remaining, 0.1)):
                    stream = key.fileobj
                    if stream is process.stdin:
                        try:
                            cursor += os.write(stream.fileno(), data[cursor:cursor + 65536])
                        except BrokenPipeError:
                            cursor = len(data)
                        if cursor == len(data):
                            selector.unregister(stream); stream.close()
                        continue
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        selector.unregister(stream); stream.close()
                        continue
                    total += len(chunk)
                    if total > MAX_OUTPUT:
                        raise PublicationError("local_git_output_limit")
                    if stream is process.stdout:
                        output.extend(chunk)
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                raise PublicationError("local_git_failed")
            return bytes(output)
    except BaseException:
        # This is the directly owned fixed Git child, never a recovered PID.
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        raise
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()


def produce(stage: Path, source: SourceGit, preview: dict, patch: bytes, before_command) -> dict:
    """Only called after a current exact local_host_execution approval."""
    if stage.exists():
        raise PublicationError("local_producer_reconciliation_required")
    stage.mkdir(mode=0o700)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(stage), "LANG": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_AUTHOR_NAME": "Seraph", "GIT_AUTHOR_EMAIL": "seraph@localhost", "GIT_COMMITTER_NAME": "Seraph", "GIT_COMMITTER_EMAIL": "seraph@localhost", "GIT_AUTHOR_DATE": preview["commit_date"], "GIT_COMMITTER_DATE": preview["commit_date"]}
    deadline = time.monotonic() + 30

    def git(*args, data=None):
        before_command()
        return _bounded_git(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null", "-c", "commit.gpgSign=false", "-c", "protocol.allow=never", *args], stage=stage, env=env, data=data, deadline=deadline)

    git("init", "--template=", "--initial-branch=seraph-publication")
    tree_id, base_files, contents = source.tree(preview["base_commit"])
    equivalent(base_files, preview["tested_input"]["base_files"])
    # Copy validated objects, never source config/index/hooks/filter rules.
    for identity, (kind, raw) in source.objects.items():
        destination = stage / ".git/objects" / identity[:2] / identity[2:]
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes(zlib.compress(f"{kind} {len(raw)}\0".encode() + raw))
    git("read-tree", preview["base_commit"])
    for path, raw in contents.items():
        target = stage / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        target.chmod(0o755 if next(item["mode"] for item in base_files if item["path"] == path) == "100755" else 0o644)
    git("apply", "--check", "--", "-", data=patch)
    git("apply", "--whitespace=nowarn", "--", "-", data=patch)
    output_files = file_manifest(stage)
    equivalent(output_files, preview["tested_input"]["tested_files"])
    # Use hash-object/update-index so .gitattributes can never invoke filters.
    entries = []
    for item in output_files:
        content = (stage / item["path"]).read_bytes()
        blob = git("hash-object", "-w", "--stdin", data=content).decode().strip()
        entries.append(f"{item['mode']} {blob}\t{item['path']}\0".encode())
    git("read-tree", "--empty")
    git("update-index", "-z", "--index-info", data=b"".join(entries))
    output_tree = git("write-tree").decode().strip()
    commit = git("commit-tree", output_tree, "-p", preview["base_commit"], data=(preview["commit_message"] + "\n").encode()).decode().strip()
    git("update-ref", "refs/heads/" + preview["branch_name"], commit)
    changed = sorted(path for path in set(contents) | {item["path"] for item in output_files} if next((item for item in base_files if item["path"] == path), None) != next((item for item in output_files if item["path"] == path), None))
    return {"local_commit": commit, "tree": output_tree, "base_tree": tree_id, "files": output_files, "changed_paths": changed, "stage": str(stage)}
