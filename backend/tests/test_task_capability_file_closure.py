"""Actual private writes/closure faults; no service or authority fixtures."""
import hashlib
import copy
import os
import stat

import pytest

from src.work_board import input_artifacts as files


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.setattr(files.settings, "workspace_dir", str(tmp_path))
    path = tmp_path / "artifacts/work-board/evidence/report.txt"
    return path, b"Local evidence report\nMemory: no_learning\n"


def test_actual_write_readback_and_exact_replay(target):
    path, payload = target
    owner = files._PayloadClosureOwner(path, payload)
    assert files._write_payload(path, payload, _closure_owner=owner) is None
    proof = owner.witness
    assert proof is not None and not proof.replayed
    assert files._verified_payload_closure(owner) is proof
    for copied in (copy.copy(owner), copy.deepcopy(owner)):
        with pytest.raises(OSError, match="original_private_payload_closure_required"):
            files._verified_payload_closure(copied)
    forged = files._PayloadClosureOwner(path, payload)
    forged.witness = proof
    with pytest.raises(OSError, match="original_private_payload_closure_required"):
        files._verified_payload_closure(forged)
    assert path.read_bytes() == payload and proof.payload_sha256 == hashlib.sha256(payload).hexdigest()
    metadata = path.stat()
    assert proof.file_identity == (metadata.st_dev, metadata.st_ino, os.getuid(), 0o600, len(payload))
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    replay = files._PayloadClosureOwner(path, payload)
    files._write_payload(path, payload, _closure_owner=replay)
    assert replay.witness is not None and replay.witness.replayed
    assert replay.witness.file_identity == proof.file_identity
    assert not list(path.parent.glob(".*.tmp"))
    with pytest.raises(ValueError, match="binding changed"):
        files._write_payload(path, payload, _closure_owner=owner)


def test_parent_close_uncertainty_has_no_positive_witness(target, monkeypatch):
    path, payload = target
    files._write_payload(path, payload)
    parent_inode = path.parent.stat().st_ino
    original = files.os.close
    closes = []
    def close(fd):
        unknown = os.fstat(fd).st_ino == parent_inode
        original(fd)
        if unknown:
            closes.append(fd)
            raise OSError("injected close uncertainty")
    monkeypatch.setattr(files.os, "close", close)
    owner = files._PayloadClosureOwner(path, payload)
    with pytest.raises(OSError, match="private_payload_closure_unknown"):
        files._write_payload(path, payload, _closure_owner=owner)
    assert owner.witness is None and len(closes) == 1
    assert path.read_bytes() == payload
    assert files._write_payload(path, payload) is None  # legacy suppression preserved


def test_primary_publication_error_preserved_when_temp_cleanup_fails(target, monkeypatch):
    path, payload = target
    primary = OSError("original publication failure")
    original = files.os.unlink
    def failed_link(*args, **kwargs):
        raise primary
    def unlink(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("injected cleanup uncertainty")
    monkeypatch.setattr(files.os, "link", failed_link)
    monkeypatch.setattr(files.os, "unlink", unlink)
    owner = files._PayloadClosureOwner(path, payload)
    with pytest.raises(OSError) as caught:
        files._write_payload(path, payload, _closure_owner=owner)
    assert caught.value is primary
    assert caught.value.__notes__ == ["private_payload_closure_unknown"]
    assert owner.witness is None and not path.exists()


def test_handle_close_failure_propagates_before_publication(target, monkeypatch):
    path, payload = target
    primary = OSError("original data handle close failure")
    original = files.os.fdopen
    class Handle:
        def __init__(self, fd, *args, **kwargs):
            self.handle = original(fd, *args, **kwargs)
        def __enter__(self):
            return self.handle
        def __exit__(self, *args):
            self.handle.close()
            raise primary
    monkeypatch.setattr(files.os, "fdopen", Handle)
    owner = files._PayloadClosureOwner(path, payload)
    with pytest.raises(OSError) as caught:
        files._write_payload(path, payload, _closure_owner=owner)
    assert caught.value is primary and owner.witness is None
    assert not path.exists() and not list(path.parent.glob(".*.tmp"))


def test_directory_fsync_failure_after_publication_is_not_success(target, monkeypatch):
    path, payload = target
    original = files.os.fsync
    primary = OSError("original directory fsync failure")
    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise primary
        return original(fd)
    monkeypatch.setattr(files.os, "fsync", fsync)
    owner = files._PayloadClosureOwner(path, payload)
    with pytest.raises(OSError) as caught:
        files._write_payload(path, payload, _closure_owner=owner)
    assert caught.value is primary and owner.witness is None
    assert path.read_bytes() == payload  # uncertain output retained, never silently removed


def test_collision_and_symlink_deny_native_witness(target):
    path, payload = target
    files._write_payload(path, payload)
    other = files._PayloadClosureOwner(path, b"changed")
    with pytest.raises(OSError, match="collision"):
        files._write_payload(path, b"changed", _closure_owner=other)
    assert other.witness is None and path.read_bytes() == payload
    alias = path.with_name("alias.txt")
    alias.symlink_to(path)
    owner = files._PayloadClosureOwner(alias, payload)
    with pytest.raises(OSError):
        files._write_payload(alias, payload, _closure_owner=owner)
    assert owner.witness is None and alias.is_symlink()
