from __future__ import annotations

import hashlib

import pytest

from src.api.workflows import (
    RepoChangeCancelRequest,
    RepoChangePreviewRequest,
    _repo_change_job_id,
    _repo_change_safe_relative,
)


def test_repo_change_request_rejects_unknown_execution_controls():
    with pytest.raises(ValueError):
        RepoChangePreviewRequest(
            goal_id="goal/1",
            goal_revision=1,
            candidate_id="candidate/1",
            idempotency_key="uuid-1",
            repository_path="repos/example",
            patch_artifact_id="art_" + "a" * 24,
            patch_sha256="a" * 64,
            allowed_paths=["src/app.py"],
            test_args=["src/app.py"],
            command="rm -rf /",
        )


def test_repo_change_identity_is_deterministic_and_owner_bound():
    first = _repo_change_job_id("operator:one", "same-key")
    second = _repo_change_job_id("operator:one", "same-key")
    other_owner = _repo_change_job_id("operator:two", "same-key")
    assert first == second
    assert first != other_owner


def test_repo_change_paths_block_escape():
    assert _repo_change_safe_relative("src/app.py", field_name="path") == "src/app.py"
    with pytest.raises(Exception):
        _repo_change_safe_relative("../secrets", field_name="path")
    with pytest.raises(Exception):
        _repo_change_safe_relative("/etc/passwd", field_name="path")


def test_cancel_reason_is_bounded():
    assert RepoChangeCancelRequest().reason == "operator_requested_stop"
    with pytest.raises(ValueError):
        RepoChangeCancelRequest(reason="x" * 161)
