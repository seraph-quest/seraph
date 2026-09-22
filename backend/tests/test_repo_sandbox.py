from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from config.settings import RepoSandboxSettings
from src.execution.repo_sandbox import (
    RepoSandboxError,
    RepoSandboxLimits,
    RootlessDockerRepoSandbox,
    SnapshotEntry,
    _digest_entries,
    validate_archive_members,
)
from src.execution.repo_worker import tree_digest


def _settings(**overrides) -> RepoSandboxSettings:
    values = {
        "enabled": True,
        "docker_socket": "unix:///run/user/1000/docker.sock",
        "worker_image_digest": "ghcr.io/operator/seraph-repo-python-pytest@sha256:" + "a" * 64,
        "profile": "repo-python-pytest-v1",
    }
    values.update(overrides)
    return RepoSandboxSettings(**values)


def test_limits_are_fixed_profile_values():
    with pytest.raises(ValueError):
        RepoSandboxLimits.from_settings(_settings(max_output_bytes=RepoSandboxLimits().max_output_bytes + 1))


def test_profile_has_no_network_and_loader_is_writable():
    runner = RootlessDockerRepoSandbox(_settings())
    loader = runner.build_loader_argv(name="seraph-repo-loader", input_volume="seraph-repo-input")
    worker = runner.build_worker_argv(name="seraph-repo-worker", input_volume="seraph-repo-input")
    assert "--network=none" in loader
    assert "--network=none" in worker
    assert not any("readonly" in value for value in loader)
    assert any("readonly" in value for value in worker)
    assert "--pull=never" in worker
    assert "--privileged" not in worker
    assert "--volume" not in worker


def test_backend_and_worker_snapshot_digest_match(tmp_path: Path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "pkg").mkdir()
    (source / "pkg" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "tests.py").write_text("assert True\n", encoding="utf-8")
    entries = []
    for path in sorted(source.rglob("*")):
        if path.is_file():
            payload = path.read_bytes()
            entries.append(
                SnapshotEntry(
                    path.relative_to(source).as_posix(),
                    len(payload),
                    hashlib.sha256(payload).hexdigest(),
                )
            )
    backend_digest = _digest_entries(entries)
    assert backend_digest == tree_digest(source)


def test_socket_and_image_validation_fail_closed():
    with pytest.raises(RepoSandboxError):
        RootlessDockerRepoSandbox.validate_socket("tcp://127.0.0.1:2375")
    with pytest.raises(RepoSandboxError):
        RootlessDockerRepoSandbox.validate_image_digest("ghcr.io/operator/seraph:latest")


def test_archive_rejects_links_and_traversal():
    import io
    import tarfile

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:") as archive:
        info = tarfile.TarInfo("../escape")
        info.size = 0
        archive.addfile(info)
    with pytest.raises(RepoSandboxError):
        validate_archive_members(payload.getvalue(), max_bytes=1024)


def test_disabled_preflight_does_not_probe_docker():
    runner = RootlessDockerRepoSandbox(_settings(enabled=False))
    result = runner.preflight()
    assert result.status == "blocked"
    assert result.reason == "repo_sandbox_disabled"
