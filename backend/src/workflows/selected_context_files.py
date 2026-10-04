"""Focused no-follow private selected-text storage; never called in SQL writers."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat

from config.settings import settings
from src.vault import encrypt, decrypt
from src.workspace import canonical_workspace_root
from src.workflows.selected_context_contract import deny, MAX_TEXT_BYTES

ROOT = "artifacts/context/private/selected-text"
MAX_CIPHERTEXT = 65536


def path_for_job(job_id):
    return ROOT + "/" + hashlib.sha256(job_id.encode()).hexdigest()[:32] + ".enc"


def _parts(path):
    parts = PurePosixPath(path).parts
    root = PurePosixPath(ROOT).parts
    if len(parts) != len(root) + 1 or parts[:-1] != root or not re.fullmatch(r"[a-f0-9]{32}\.enc", parts[-1]):
        raise OSError("selected context artifact path invalid")
    return parts


@contextmanager
def parent(path, *, create=False):
    parts = _parts(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root = canonical_workspace_root(settings.workspace_dir)
    descriptors = [os.open(root, flags)]
    try:
        if os.fstat(descriptors[0]).st_uid != os.getuid():
            raise OSError("selected context workspace owner invalid")
        for index, part in enumerate(parts[:-1]):
            try:
                descriptor = os.open(part, flags, dir_fd=descriptors[-1])
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptors[-1])
                except FileExistsError:
                    pass
                descriptor = os.open(part, flags, dir_fd=descriptors[-1])
            descriptors.append(descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or (index > 0 and stat.S_IMODE(metadata.st_mode) != 0o700):
                raise OSError("selected context private directory invalid")
        yield descriptors[-1], parts[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _private_file(descriptor):
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
        raise OSError("selected context private file invalid")
    return metadata


@contextmanager
def capture_lock(job_id, *, shared=False):
    # Stable lock inode is retained; unlinking it could split concurrent owners.
    with parent(path_for_job(job_id), create=True) as (directory, name):
        descriptor = os.open(name + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        locked = False
        try:
            _private_file(descriptor)
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                deny("selected_context_capture_busy", 503)
            yield
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def prepare(job_id, text):
    raw = text.encode("utf-8", errors="strict")
    if not 1 <= len(raw) <= MAX_TEXT_BYTES:
        raise OSError("selected context text bound exceeded")
    ciphertext = encrypt(json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":"))).encode()
    if not 1 <= len(ciphertext) <= MAX_CIPHERTEXT:
        raise OSError("selected context ciphertext bound exceeded")
    return {"path": path_for_job(job_id), "ciphertext_digest": hashlib.sha256(ciphertext).hexdigest(),
        "ciphertext_bytes": len(ciphertext), "plaintext_digest": hashlib.sha256(raw).hexdigest(), "plaintext_bytes": len(raw)}, ciphertext


def read_ciphertext(ref):
    with parent(ref["path"]) as (directory, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        try:
            before = _private_file(descriptor)
            if not 1 <= before.st_size <= MAX_CIPHERTEXT:
                raise OSError("selected context ciphertext bound invalid")
            raw = bytearray()
            while len(raw) <= MAX_CIPHERTEXT:
                part = os.read(descriptor, min(8192, MAX_CIPHERTEXT + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            after = _private_file(descriptor)
            if before.st_size != after.st_size or len(raw) != before.st_size or len(raw) != ref["ciphertext_bytes"] or hashlib.sha256(raw).hexdigest() != ref["ciphertext_digest"]:
                raise OSError("selected context ciphertext changed")
            return bytes(raw)
        finally:
            os.close(descriptor)


def publish(ref, ciphertext):
    if hashlib.sha256(ciphertext).hexdigest() != ref["ciphertext_digest"]:
        raise OSError("selected context staging changed")
    with parent(ref["path"], create=True) as (directory, name):
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        except FileExistsError:
            if read_ciphertext(ref) != ciphertext:
                raise OSError("selected context existing artifact differs")
            return
        try:
            _private_file(descriptor)
            offset = 0
            while offset < len(ciphertext):
                written = os.write(descriptor, ciphertext[offset:])
                if written <= 0:
                    raise OSError("selected context short write")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory)


def read(ref):
    value = json.loads(decrypt(read_ciphertext(ref).decode()))
    if not isinstance(value, dict) or set(value) != {"text"} or not isinstance(value["text"], str):
        raise OSError("selected context private schema invalid")
    raw = value["text"].encode("utf-8", errors="strict")
    if len(raw) != ref["plaintext_bytes"] or hashlib.sha256(raw).hexdigest() != ref["plaintext_digest"]:
        raise OSError("selected context private readback changed")
    return value["text"]


def discard(ref):
    """Verify named absence after deleting only this exact reserved ciphertext."""
    try:
        read_ciphertext(ref)
    except FileNotFoundError:
        return True
    with parent(ref["path"]) as (directory, name):
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return True
    raise OSError("selected context cleanup unverified")
