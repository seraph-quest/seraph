"""Actual tracked Node source and separately frozen installed dependencies."""
import json
from pathlib import Path
import stat

import pytest

from src.workflows.repo_repair import RepoRepairService, RepoWorkInput, RepoRepairError
from src.work_board.contracts import WorkBoardOwner
from src.execution.repo_sandbox import RepoSandboxError
from tests.test_repo_node import make_fixture
from tests.test_repo_work_source import git
from tests.test_repo_work_contracts import selection


@pytest.fixture
def node_source(tmp_path):
    executor, repository, _ = make_fixture(tmp_path, native=False)
    (repository / '.gitignore').write_text('node_modules/\n')
    dependencies = repository / 'node_modules/typescript/bin'
    dependencies.mkdir(parents=True)
    (dependencies / 'tsc').write_text('/* inspected unused dependency */\n')
    (dependencies / 'tsc').chmod(0o755)
    aliases = repository / 'node_modules/.bin'
    aliases.mkdir()
    (aliases / 'tsc').symlink_to('../typescript/bin/tsc')
    (repository / 'src/app.js').chmod(0o755)
    git(repository, 'init', '--template=', '--initial-branch=develop')
    git(repository, 'add', '.')
    git(repository, 'commit', '-m', 'original tracked Node source')
    base = git(repository, 'rev-parse', 'HEAD').decode().strip()
    service = RepoRepairService(sandbox=executor, workspace_dir=str(repository.parent))
    work = RepoWorkInput.model_validate(selection(repository_ref='repo', base_commit=base,
        allowed_paths=['src/app.js', 'tests/app.test.js'], language_profile='test_node'))
    return repository, service, work


def staged(service, work):
    binding = service.stage_task_source(work, owner=WorkBoardOwner(principal_id='owner', session_id='session'),
        goal_id='goal', goal_revision=1)
    return binding, json.loads(service._read_private_artifact(binding.source_artifact_ref,
        expected_digest=binding.source_artifact_digest))


def test_actual_node_source_and_dependency_alias_are_separately_frozen(node_source, tmp_path):
    repository, service, work = node_source
    binding, facts = staged(service, work)
    assert not any(row['path'].startswith('node_modules/') for row in facts['git_manifest'])
    alias = next(row for row in facts['snapshot_manifest']['entries'] if row['path'] == 'node_modules/.bin/tsc')
    assert alias['kind'] == 'alias:../typescript/bin/tsc'
    assert service.recheck_task_source_snapshot(work, facts).test_args == ['npm', 'test']
    copied = tmp_path / 'copy'
    service.sandbox.snapshot_repository(repository, copied, preserve_source_modes=True)
    assert stat.S_IMODE((copied / 'src/app.js').stat().st_mode) == 0o700
    assert stat.S_IMODE((copied / 'tests/app.test.js').stat().st_mode) == 0o600
    assert stat.S_IMODE((copied / 'node_modules/typescript/bin/tsc').stat().st_mode) == 0o600
    assert not (copied / 'node_modules/.bin/tsc').is_symlink()
    assert (repository / 'node_modules/.bin/tsc').is_symlink()


@pytest.mark.parametrize('change', ['dependency_bytes', 'alias_escape', 'untracked', 'source_mode', 'dependency_root_link'])
def test_node_original_source_or_dependency_drift_cannot_qualify(node_source, change, tmp_path):
    repository, service, work = node_source
    _, facts = staged(service, work)
    if change == 'dependency_bytes':
        (repository / 'node_modules/typescript/bin/tsc').write_text('CHANGED\n')
    elif change == 'alias_escape':
        alias = repository / 'node_modules/.bin/tsc'
        alias.unlink()
        alias.symlink_to(tmp_path / 'outside')
        (tmp_path / 'outside').write_text('outside\n')
    elif change == 'untracked':
        (repository / 'operator.js').write_text('preserve operator work\n')
    elif change == 'source_mode':
        (repository / 'src/app.js').chmod(0o644)
    else:
        import shutil
        shutil.rmtree(repository / 'node_modules')
        (repository / 'node_modules').symlink_to(tmp_path)
    with pytest.raises((RepoRepairError, RepoSandboxError, OSError)):
        service.recheck_task_source_snapshot(work, facts)


def test_tracked_node_modules_never_becomes_source_selection(node_source):
    repository, service, work = node_source
    git(repository, 'add', '-f', 'node_modules/typescript/bin/tsc')
    git(repository, 'commit', '-m', 'forbidden tracked dependency')
    work = work.model_copy(update={'base_commit': git(repository, 'rev-parse', 'HEAD').decode().strip()})
    with pytest.raises(RepoRepairError) as denied:
        service.inspect_work_selection(work)
    assert denied.value.code == 'repo_work_tracked_dependencies'
