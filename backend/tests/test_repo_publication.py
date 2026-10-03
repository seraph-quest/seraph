"""Native Git mechanics and exact publication boundary negatives; no accounts."""
from pathlib import Path
import subprocess

import pytest

from src.execution.repo_publication import SourceGit, PublicationError, equivalent, file_manifest, produce
from src.extensions.github_followthrough import GitHubFollowthroughService, GitHubFollowthroughError
from src.workflows.repo_publication import PrepareRequest


def git(root, *args):
    result = subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", *args], cwd=root, capture_output=True, check=True, env={"PATH": "/usr/bin:/bin", "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@localhost", "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@localhost"})
    return result.stdout


def fixture_repo(tmp_path):
    root = tmp_path / "source"; root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n")
    (root / "test_app.py").write_text("from app import VALUE\ndef test_value():\n    assert VALUE == 2\n")
    git(root, "init", "--template=", "--initial-branch=develop")
    git(root, "add", "."); git(root, "commit", "-m", "base")
    base = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "app.py").write_text("VALUE = 2\n")
    patch = git(root, "diff", "--binary")
    patched = file_manifest(root)
    (root / "app.py").write_text("VALUE = 1\n")
    source = SourceGit(root)
    tree, baseline, _ = source.tree(base)
    preview = {"base_commit": base, "base_tree": tree, "branch_name": "feat/tested-patch", "commit_message": "Bounded fixture repair", "commit_date": "2026-10-02T10:00:00+00:00", "tested_input": {"base_files": baseline, "tested_files": patched}}
    return root, source, preview, patch


def test_actual_local_commit_and_real_bare_remote_readback_preserve_source(tmp_path):
    root, source, preview, patch = fixture_repo(tmp_path)
    original_head = source.head()
    original_bytes = file_manifest(root)
    calls = []
    stage = tmp_path / "producer"
    result = produce(stage, source, preview, patch, lambda: calls.append("exact_authority_check"))
    assert len(calls) >= 8
    assert source.head() == original_head and file_manifest(root) == original_bytes
    assert git(stage, "show", result["local_commit"] + ":app.py") == b"VALUE = 2\n"
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "--template=", str(bare))
    git(stage, "-c", "protocol.file.allow=always", "push", str(bare), "refs/heads/feat/tested-patch:refs/heads/feat/tested-patch")
    assert git(bare, "rev-parse", "refs/heads/feat/tested-patch").decode().strip() == result["local_commit"]
    assert git(bare, "show", "refs/heads/feat/tested-patch:app.py") == b"VALUE = 2\n"
    assert git(bare, "rev-parse", "refs/heads/feat/tested-patch^{tree}").decode().strip() == result["tree"]


def test_actual_git_output_is_bounded_while_child_runs(tmp_path, monkeypatch):
    import time
    from src.execution import repo_publication as module
    root, _, _, _ = fixture_repo(tmp_path)
    monkeypatch.setattr(module, "MAX_OUTPUT", 64)
    with pytest.raises(PublicationError, match="local_git_output_limit"):
        module._bounded_git(["/usr/bin/git", "show", "HEAD"], stage=root, env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}, data=None, deadline=time.monotonic() + 5)


@pytest.mark.parametrize("mismatch", ["extra", "byte", "mode", "missing", "subtree", "commit"])
def test_actual_capture_git_base_mismatches_fail_closed(tmp_path, mismatch):
    root, source, preview, patch = fixture_repo(tmp_path)
    if mismatch == "extra":
        (root / "ignored-input.cfg").write_text("A test-only dependency absent from Git\n")
        actual = file_manifest(root)
    elif mismatch == "byte":
        (root / "app.py").write_text("VALUE = 3\n"); actual = file_manifest(root)
    elif mismatch == "mode":
        (root / "app.py").chmod(0o755); actual = file_manifest(root)
    elif mismatch == "missing":
        (root / "test_app.py").unlink(); actual = file_manifest(root)
    elif mismatch == "subtree":
        actual = [item for item in file_manifest(root) if item["path"] == "app.py"]
    else:
        (root / "other.py").write_text("OTHER = 1\n")
        git(root, "add", "."); git(root, "commit", "-m", "unapproved base")
        _, actual, _ = SourceGit(root).tree(SourceGit(root).head())
    with pytest.raises(PublicationError, match="tested_base_equivalence_mismatch"):
        equivalent(actual, preview["tested_input"]["base_files"])
    assert not (tmp_path / "producer").exists()


def test_operator_git_config_filters_and_hooks_never_imported(tmp_path):
    root, source, preview, patch = fixture_repo(tmp_path)
    marker = tmp_path / "hook-ran"
    (root / ".git/hooks").mkdir()
    (root / ".git/hooks/pre-commit").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (root / ".git/hooks/pre-commit").chmod(0o755)
    git(root, "config", "core.hooksPath", str(root / ".git/hooks"))
    git(root, "config", "filter.malicious.clean", f"touch {marker}")
    result = produce(tmp_path / "producer", source, preview, patch, lambda: None)
    assert result["local_commit"] and not marker.exists()
    config = (tmp_path / "producer/.git/config").read_text()
    assert "malicious" not in config and str(root) not in config


def test_alternate_or_linked_git_store_is_unavailable(tmp_path):
    root, _, _, _ = fixture_repo(tmp_path)
    (root / ".git/objects/info/alternates").write_text("/private/operator/objects\n")
    with pytest.raises(PublicationError, match="alternate_or_shallow_repository_unsupported"):
        SourceGit(root)


@pytest.mark.asyncio
async def test_adapter_without_final_authority_callback_sends_zero_bytes():
    service = GitHubFollowthroughService()
    # Baseline does not yet have reviewed #910 callback. This is fail-closed
    # dependency proof, not a substituted successful REST journey.
    import inspect
    if "authority_check" in inspect.signature(service._request).parameters:
        pytest.skip("reviewed final-authority callback dependency is integrated")
    with pytest.raises(GitHubFollowthroughError, match="publication_transport_authority_unavailable"):
        await service.request_repo_publication("/repos/acme/example/git/refs", method="POST", token="fixture-key", json_body={"ref": "refs/heads/feat/fixture", "sha": "a" * 40}, authority_check=lambda: None)


def test_publication_request_cannot_supply_grant_credential_or_caller_artifacts():
    values = {"repair_job_id": "repair", "expected_repair_revision": 1, "proposal_id": "proposal", "expected_proposal_revision": 1, "expected_connection_revision": 1, "base_branch": "develop", "expected_base_commit": "a" * 40, "branch_name": "feat/fix", "commit_message": "Fix", "title": "Fix", "body": "Reviewed public text", "idempotency_key": "11111111-1111-4111-8111-111111111111"}
    for field in ("approved", "credential", "artifact_path", "transport", "owner_principal_id"):
        with pytest.raises(ValueError):
            PrepareRequest(**{**values, field: True})
