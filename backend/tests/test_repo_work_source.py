"""Actual Git source inspection, with dirty/drifted source never overwritten."""
from pathlib import Path
import subprocess

import pytest

from config.settings import RepoSandboxSettings
from src.execution.repo_sandbox import LocalRepoRepairExecutor
from src.workflows.repo_repair import RepoRepairError, RepoRepairService, RepoWorkInput
from tests.test_repo_work_contracts import selection


def git(root: Path, *args):
    return subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args],
        cwd=root, check=True, capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@localhost", "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@localhost"}).stdout


@pytest.fixture
def inspected_repository(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    repository = workspace / "example"
    repository.mkdir(mode=0o700)
    (repository / "tests").mkdir()
    (repository / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    (repository / "tests/test_calculator.py").write_text("from calculator import add\ndef test_add():\n    assert add(1, 2) == 3\n")
    git(repository, "init", "--template=", "--initial-branch=develop")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "base")
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    sandbox = LocalRepoRepairExecutor(RepoSandboxSettings(enabled=True, executor_kind="local",
        profile="repo-python-pytest-v1"), workspace_dir=workspace)
    service = RepoRepairService(sandbox=sandbox, workspace_dir=str(workspace))
    work = RepoWorkInput.model_validate(selection(repository_ref="example", base_commit=base))
    return repository, service, work


def test_actual_inspection_compiles_existing_profile_without_executing_source(inspected_repository):
    repository, service, work = inspected_repository
    before = (repository / "calculator.py").read_bytes()
    compiled = service.inspect_work_selection(work)
    assert compiled.repository_path == "example"
    assert compiled.test_args == ["pytest", "-q", "tests/test_calculator.py"]
    assert (repository / "calculator.py").read_bytes() == before
    assert git(repository, "rev-parse", "HEAD").decode().strip() == work.base_commit


@pytest.mark.parametrize("change", ["dirty", "untracked", "mode", "commit", "symlink"])
def test_real_source_drift_blocks_and_preserves_live_files(inspected_repository, change):
    repository, service, work = inspected_repository
    source = repository / "calculator.py"
    if change == "dirty":
        source.write_text("OPERATOR_CHANGE = True\n")
    elif change == "untracked":
        (repository / "operator.txt").write_text("Keep this file\n")
    elif change == "mode":
        source.chmod(0o755)
    elif change == "symlink":
        source.unlink()
        source.symlink_to("tests/test_calculator.py")
    else:
        source.write_text("OTHER_BASE = True\n")
        git(repository, "add", ".")
        git(repository, "commit", "-m", "changed base")
    before = source.read_bytes()
    with pytest.raises(RepoRepairError, match="selected Git base changed|clean supported Git base"):
        service.inspect_work_selection(work)
    assert source.read_bytes() == before
    if change == "symlink":
        assert source.is_symlink()
