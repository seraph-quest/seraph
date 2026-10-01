from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from config.settings import RepoSandboxSettings, settings
from src.execution.repo_sandbox import (
    RepoSandboxError,
    RepoSandboxLimits,
    RepoSandboxJob,
    RootlessDockerRepoSandbox,
    SnapshotEntry,
    _patch_paths_from_diff,
    _digest_entries,
    _open_source_regular_file as _open_sandbox_source_regular_file,
    validate_archive_members,
)
from src.execution.repo_worker import (
    MAX_ALLOWED_PATH_BYTES,
    MAX_ALLOWED_PATHS,
    MAX_JOB_BYTES,
    WorkerInputError,
    _open_source_regular_file as _open_worker_source_regular_file,
    _read_bounded_job_json,
    _validate_allowed_paths,
    _validate_changed_paths,
    tree_digest,
)


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


def test_worker_job_descriptor_is_bounded_before_json_parse(tmp_path: Path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    job_file = input_root / "job.json"
    job_file.write_bytes(b"{" + b"x" * MAX_JOB_BYTES)

    with pytest.raises(WorkerInputError, match="job input exceeds"):
        _read_bounded_job_json(job_file)


def test_worker_allowed_paths_are_typed_unique_and_bounded():
    assert _validate_allowed_paths(["src/app.py"]) == {"src/app.py"}
    invalid_values = (
        None,
        "src/app.py",
        ("src/app.py",),
        ["src/app.py", 7],
        ["src/app.py", "src/app.py"],
        ["x" * (MAX_ALLOWED_PATH_BYTES + 1)],
        [f"src/{index}.py" for index in range(MAX_ALLOWED_PATHS + 1)],
    )
    for value in invalid_values:
        with pytest.raises(WorkerInputError, match="allowed_paths"):
            _validate_allowed_paths(value)


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


def _rootless_info(**overrides):
    value = {
        "OSType": "linux",
        "ServerRootless": True,
        "CgroupVersion": "2",
        "CgroupDriver": "systemd",
        "CpuCfsQuota": True,
        "CpuCfsPeriod": True,
        "MemoryLimit": True,
        "SwapLimit": True,
        "PidsLimit": True,
        "Warnings": [],
    }
    value.update(overrides)
    return value


def _patch_preflight_docker(monkeypatch: pytest.MonkeyPatch, info: dict) -> list[list[str]]:
    image = _settings().worker_image_digest
    calls: list[list[str]] = []

    def run(_runner, args, **_kwargs):
        calls.append(list(args))
        if args[:2] == ["info", "--format"]:
            return 0, json.dumps(info).encode(), b""
        if args[:3] == ["image", "inspect", "--format"]:
            return 0, json.dumps({"RepoDigests": [image], "Id": image.rsplit("/", 1)[-1]}).encode(), b""
        raise AssertionError(f"unexpected Docker call: {args}")

    monkeypatch.setattr(RootlessDockerRepoSandbox, "_run_docker", run)
    return calls


@pytest.mark.parametrize(
    ("info_overrides", "expected"),
    (
        ({"CpuCfsQuota": False}, "resource_controller_unavailable:cpu"),
        ({"CpuCfsPeriod": False}, "resource_controller_unavailable:cpu"),
        ({"MemoryLimit": False}, "resource_controller_unavailable:memory"),
        ({"SwapLimit": False}, "resource_controller_unavailable:memory"),
        ({"PidsLimit": False}, "resource_controller_unavailable:pids"),
        ({"CpuCfsQuota": "true"}, "resource_controller_unavailable:cpu"),
        ({"CpuCfsQuota": None}, "resource_controller_unavailable:cpu"),
    ),
)
def test_preflight_blocks_when_effective_resource_controller_is_missing_or_unknown(
    monkeypatch: pytest.MonkeyPatch,
    info_overrides: dict,
    expected: str,
):
    calls = _patch_preflight_docker(monkeypatch, _rootless_info(**info_overrides))

    result = RootlessDockerRepoSandbox(_settings()).preflight()

    assert not result.ok
    assert result.status == "blocked"
    assert result.reason == expected
    assert result.as_receipt()["reason"] == expected
    assert result.as_receipt()["support_confirmed"] is False
    assert calls == [["info", "--format", "{{json .}}"]]


def test_preflight_requires_declared_cgroup_version_and_systemd_driver(monkeypatch: pytest.MonkeyPatch):
    for overrides in ({"CgroupVersion": "1"}, {"CgroupDriver": "cgroupfs"}, {}):
        info = _rootless_info(**overrides)
        if not overrides:
            info.pop("CpuCfsQuota")
        calls = _patch_preflight_docker(monkeypatch, info)

        result = RootlessDockerRepoSandbox(_settings()).preflight()

        assert result.reason == "resource_controller_unavailable:cpu"
        assert calls == [["info", "--format", "{{json .}}"]]


def test_preflight_receipt_keeps_verified_resource_evidence_and_then_checks_image(monkeypatch: pytest.MonkeyPatch):
    calls = _patch_preflight_docker(monkeypatch, _rootless_info())

    result = RootlessDockerRepoSandbox(_settings()).preflight()

    assert result.ok
    assert result.status == "ready"
    receipt = result.as_receipt()
    assert receipt["cgroup_version"] == "2"
    assert receipt["cgroup_driver"] == "systemd"
    assert receipt["daemon_reported"] == {
        "CpuCfsQuota": True,
        "CpuCfsPeriod": True,
        "MemoryLimit": True,
        "SwapLimit": True,
        "PidsLimit": True,
    }
    assert receipt["support_confirmed"] is True
    assert calls == [
        ["info", "--format", "{{json .}}"],
        ["image", "inspect", "--format", "{{json .}}", _settings().worker_image_digest],
    ]


def test_preflight_warning_is_supplemental_when_typed_support_is_present(monkeypatch: pytest.MonkeyPatch):
    calls = _patch_preflight_docker(
        monkeypatch,
        _rootless_info(Warnings=["WARNING: daemon warning with an unstable format"]),
    )

    result = RootlessDockerRepoSandbox(_settings()).preflight()

    assert result.ok
    assert result.as_receipt()["support_confirmed"] is True
    assert calls[-1][0:2] == ["image", "inspect"]


def test_exported_diff_paths_are_bounded_by_the_allowlist():
    assert _validate_changed_paths(b"tests/test_app.py\0", {"tests/test_app.py"}) == ["tests/test_app.py"]
    assert _validate_changed_paths(
        b"tests/test_app.py\0",
        {"tests/test_app.py"},
        required_paths=("tests/test_app.py",),
    ) == ["tests/test_app.py"]
    with pytest.raises(WorkerInputError, match="missing approved patch paths"):
        _validate_changed_paths(b"", {"tests/test_app.py"}, required_paths=("tests/test_app.py",))
    with pytest.raises(WorkerInputError, match="missing approved patch paths"):
        _validate_changed_paths(b"src/other.py\0", {"src/other.py", "tests/test_app.py"}, required_paths=("tests/test_app.py",))
    with pytest.raises(WorkerInputError, match="not allowed"):
        _validate_changed_paths(b"tests/test_app.py\0src/secret.py\0", {"tests/test_app.py"})
    with pytest.raises(WorkerInputError, match="malformed"):
        _validate_changed_paths(b"tests/test_app.py", {"tests/test_app.py"})


def test_approved_patch_paths_are_bounded_by_the_allowlist():
    patch = b"--- a/tests/test_app.py\n+++ b/tests/test_app.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert _patch_paths_from_diff(patch, ("tests/test_app.py",)) == ("tests/test_app.py",)
    with pytest.raises(RepoSandboxError, match="not allowed"):
        _patch_paths_from_diff(patch.replace(b"tests/test_app.py", b"src/secret.py"), ("tests/test_app.py",))


def test_worker_output_rejects_empty_diff_for_an_approved_patch():
    runner = RootlessDockerRepoSandbox(_settings())
    image = _settings().worker_image_digest
    patch = b"--- a/tests/test_app.py\n+++ b/tests/test_app.py\n@@ -1 +1 @@\n-a\n+b\n"
    job = RepoSandboxJob(
        job_id="repo-change-" + "a" * 32,
        repository_root="repos/example",
        patch_bytes=patch,
        allowed_paths=("tests/test_app.py",),
        test_args=("tests/test_app.py",),
        authority_digest="a" * 64,
        base_digest="b" * 64,
        worker_image_digest=image,
    )
    digest = hashlib.sha256(patch).hexdigest()
    manifest = {
        "profile": "repo-python-pytest-v1",
        "status": "succeeded",
        "exit_code": 0,
        "timed_out": False,
        "base_digest": "b" * 64,
        "snapshot_digest": "b" * 64,
        "patch_sha256": digest,
        "worker_image_digest": image,
        "worker_source_digest": "c" * 64,
        "diff_sha256": hashlib.sha256(b"").hexdigest(),
        "allowed_paths": ["tests/test_app.py"],
        "patch_paths": ["tests/test_app.py"],
        "diff_paths": ["tests/test_app.py"],
        "test_args": ["tests/test_app.py"],
        "stdout_truncated": False,
        "stderr_truncated": False,
    }
    outputs = {
        "manifest.json": json.dumps(manifest).encode(),
        "readback.json": json.dumps(manifest).encode(),
        "diff.patch": b"",
        "pytest.stdout": b"",
        "pytest.stderr": b"",
    }
    with pytest.raises(RepoSandboxError, match="diff is empty"):
        runner._validate_worker_output(outputs=outputs, job=job, image=image, patch_paths=("tests/test_app.py",))


def test_snapshot_rejects_an_in_workspace_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real = workspace / "real"
    real.mkdir()
    link = workspace / "link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    with pytest.raises(RepoSandboxError, match="symlinks"):
        RootlessDockerRepoSandbox(_settings()).validate_snapshot_root(link)


def test_snapshot_rejects_hardlinked_repository_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    repository = workspace / "repo"
    repository.mkdir(parents=True)
    source = repository / "source.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    os.link(source, repository / "linked.py")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    with pytest.raises(RepoSandboxError, match="hardlink"):
        RootlessDockerRepoSandbox(_settings()).snapshot_repository(repository, tmp_path / "staging")
    with pytest.raises(WorkerInputError, match="hardlinked"):
        tree_digest(repository)


def test_sandbox_source_descriptor_rejects_identity_replacement(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "source.py"
    source.write_text("print('original')\n", encoding="utf-8")
    expected_stat = source.lstat()
    replacement = repository / "replacement.py"
    replacement.write_text("print('replacement')\n", encoding="utf-8")
    source.unlink()
    replacement.rename(source)

    with pytest.raises(RepoSandboxError, match="identity changed"):
        _open_sandbox_source_regular_file(repository, "source.py", expected_stat=expected_stat)


def test_worker_source_descriptor_rejects_identity_replacement(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "source.py"
    source.write_text("print('original')\n", encoding="utf-8")
    expected_stat = source.lstat()
    replacement = repository / "replacement.py"
    replacement.write_text("print('replacement')\n", encoding="utf-8")
    source.unlink()
    replacement.rename(source)

    with pytest.raises(WorkerInputError, match="identity changed"):
        _open_worker_source_regular_file(repository, "source.py", expected_stat=expected_stat)


def test_export_barrier_loss_is_a_failed_output_receipt(monkeypatch: pytest.MonkeyPatch):
    runner = RootlessDockerRepoSandbox(_settings())

    monkeypatch.setattr(runner, "_run_docker", lambda *_args, **_kwargs: (1, b"", b"No such object"))
    with pytest.raises(RepoSandboxError, match="output_lost") as error:
        runner._wait_for_export_ready("seraph-repo-worker", timeout=1)
    assert error.value.phase == "output_exported"
    assert error.value.terminal_status == "failed"
