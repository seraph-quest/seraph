"""Approved patch paths and the published tree cannot escape the allowlist.

git apply honours more than ``--- a/``/``+++ b/`` text sections. Every other
section kind must be rejected by both the trusted parent validator and the
worker validator, and an iterative publication must equal the base plus the
operator-approved input patch rather than worker-exported test-writable bytes.
"""
import pytest

from src.execution.repo_publication import PublicationError, _produce_direct, file_manifest
from src.execution.repo_sandbox import RepoSandboxError, _patch_paths_from_diff
from src.execution.repo_worker import WorkerInputError, _validate_patch_paths
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_publication import fixture_repo, git


ALLOWED = {"test_a.py"}
TEXT = ("--- a/test_a.py\n+++ b/test_a.py\n@@ -1,2 +1,2 @@\n def test_a():\n"
        "-    assert 1\n+    assert 2\n")
SECTIONS = {
    "headerless_new_file": "diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n@@ -0,0 +1 @@\n+print('x')\n",
    "headerless_modify": "diff --git a/package.json b/package.json\n@@ -1 +1 @@\n-{}\n+{\"scripts\": {}}\n",
    "binary_create": ("diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n"
        "index 0000000000000000000000000000000000000000..8cbad767222fa69b8950eb0ea929b056e9e83479\n"
        "GIT binary patch\nliteral 39\nucmd1IEyyn_QOGamDk#d#E74F22={Y!QAjJw&s9jy&r2&wEiO^g<N^Q|8VyVU\n\n"
        "literal 0\nHcmV?d00001\n\n"),
    "binary_rewrite": ("diff --git a/test_a.py b/test_a.py\nindex d6a29c3..d3e30e9 100644\n"
        "Binary files a/test_a.py and b/test_a.py differ\n"),
    "rename": ("diff --git a/test_a.py b/conftest.py\nsimilarity index 100%\n"
        "rename from test_a.py\nrename to conftest.py\n"),
    "copy": ("diff --git a/test_a.py b/conftest.py\nsimilarity index 100%\n"
        "copy from test_a.py\ncopy to conftest.py\n"),
    "mode_only": "diff --git a/test_a.py b/test_a.py\nold mode 100644\nnew mode 100755\n",
    "symlink": ("diff --git a/test_a.py b/test_a.py\nnew file mode 120000\n--- /dev/null\n"
        "+++ b/test_a.py\n@@ -0,0 +1 @@\n+/home/user/.ssh/id_rsa\n\\ No newline at end of file\n"),
    "executable_new": ("diff --git a/test_a.py b/test_a.py\nnew file mode 100755\n--- /dev/null\n"
        "+++ b/test_a.py\n@@ -0,0 +1 @@\n+x = 1\n"),
    "unallowed_text": "--- a/conftest.py\n+++ b/conftest.py\n@@ -1 +1 @@\n-a\n+b\n",
    "header_mismatch": ("diff --git a/test_a.py b/test_a.py\n--- a/test_a.py\n+++ b/conftest.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"),
    "trailing_garbage": "garbage outside a counted hunk\n",
    "hunk_overrun": "--- a/test_a.py\n+++ b/test_a.py\n@@ -1 +1 @@\n-a\n+b\n+c\n",
}


@pytest.mark.parametrize("section", sorted(SECTIONS))
def test_every_non_text_or_unallowed_section_is_rejected_by_both_validators(section):
    patch = (TEXT + SECTIONS[section]).encode()
    with pytest.raises(RepoSandboxError):
        _patch_paths_from_diff(patch, ALLOWED)
    with pytest.raises(WorkerInputError):
        _validate_patch_paths(patch, ALLOWED)


@pytest.mark.parametrize("patch,expected", [
    (TEXT, ["test_a.py"]),
    ("diff --git a/test_a.py b/test_a.py\nindex d6a29c3..d3e30e9 100644\n" + TEXT, ["test_a.py"]),
    ("diff --git a/test_a.py b/test_a.py\nnew file mode 100644\nindex 0000000..d3e30e9\n--- /dev/null\n"
     "+++ b/test_a.py\n@@ -0,0 +1,2 @@\n+def test_a():\n+    assert 2\n", ["test_a.py"]),
    ("--- a/test_a.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def test_a():\n-    assert 1\n", ["test_a.py"]),
    ("--- a/test_a.py\n+++ b/test_a.py\n@@ -1 +1 @@\n--- a/inner\n+++ b/inner\n", ["test_a.py"]),
    ("--- a/test_a.py\n+++ b/test_a.py\n@@ -1 +1 @@\n-x\n+y\n\\ No newline at end of file\n", ["test_a.py"]),
])
def test_text_sections_keep_their_exact_paths(patch, expected):
    assert list(_patch_paths_from_diff(patch.encode(), ALLOWED)) == expected
    assert _validate_patch_paths(patch.encode(), ALLOWED) == expected


def _iterative_preview(preview, approved):
    import hashlib
    return {**preview, "repair_binding": {"approved_input_patch_digest": hashlib.sha256(approved).hexdigest()}}


def test_iterative_publication_equals_base_plus_approved_input(tmp_path):
    root, source, preview, patch = fixture_repo(tmp_path)
    result = _produce_direct(tmp_path / "producer", source, _iterative_preview(preview, patch), patch,
        lambda: None, approved_patch=patch)
    assert git(tmp_path / "producer", "show", result["local_commit"] + ":app.py") == b"VALUE = 2\n"
    assert result["changed_paths"] == ["app.py"]


def test_iterative_publication_rejects_a_forged_tested_tree(tmp_path):
    """Test-writable exported diff and tested manifest agree, but differ from approval."""
    root, source, preview, approved = fixture_repo(tmp_path)
    (root / "app.py").write_text("VALUE = 2\n")
    (root / "conftest.py").write_text("print('persisted by a test')\n")
    git(root, "add", "-N", "conftest.py")
    forged = git(root, "diff", "--binary")
    forged_tree = file_manifest(root)
    (root / "conftest.py").unlink()
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "reset", "-q")
    forged_preview = {**preview, "tested_input": {**preview["tested_input"], "tested_files": forged_tree}}
    with pytest.raises(PublicationError, match="tested_tree_differs_from_approved_input"):
        _produce_direct(tmp_path / "producer", source, _iterative_preview(forged_preview, approved), forged,
            lambda: None, approved_patch=approved)


# The proposal paths: a model diff with an unsupported section never becomes
# an approvable proposal on either the single-shot or the iterative Source path.
_PROPOSAL_SECTIONS = ["headerless_new_file", "headerless_modify", "binary_create", "binary_rewrite",
    "rename", "copy", "mode_only", "symlink"]


def _model_patch(path, section):
    text = (f"--- a/{path}\n+++ b/{path}\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
            "-    return a - b\n+    return a + b\n") if path == "calculator.py" else (
            f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n")
    return text + SECTIONS[section].replace("test_a.py", path)


@pytest.mark.asyncio
@pytest.mark.parametrize("section", _PROPOSAL_SECTIONS)
async def test_single_shot_proposal_rejects_unsupported_patch_sections(async_db, tmp_path, monkeypatch, section):
    import json
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from config.settings import settings
    from src.execution.repo_sandbox import RootlessDockerRepoSandbox
    from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
    from src.work_board.contracts import WorkBoardOwner
    from src.workflows.repo_repair import RepoRepairError, RepoRepairService
    from tests.test_repo_repair import _repair_input, _seed_canonical_repair
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "tests").mkdir()
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n")
    (workspace / "repo" / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    now = datetime.now(timezone.utc)
    sandbox = RootlessDockerRepoSandbox()
    sandbox.preflight = lambda: SimpleNamespace(ok=True, status="verified", reason="",
        as_receipt=lambda: {"ok": True, "status": "verified", "reason": "", "operator_visible": True})
    output = {"summary": "Change the bounded value.", "base_snapshot_sha256": None,
        "patch_unified_diff": _model_patch("src/app.py", section),
        "allowed_paths": ["src/app.py", "tests/test_app.py"], "test_args": ["pytest", "tests/test_app.py"],
        "expected_outcome": "The focused test passes."}
    class Model:
        def generate(self, *_args, **_kwargs):
            return json.dumps(output, sort_keys=True)
    service = RepoRepairService(sandbox=sandbox, secret_scanner=lambda value: value, session_factory=async_db,
        workspace_dir=str(workspace), clock=lambda: now)
    owner = WorkBoardOwner(principal_id="operator:single", session_id="session:patch-boundary")
    request = _repair_input()
    async with async_db() as db:
        await _seed_canonical_repair(db, workspace, request, owner=owner, task_id="task:patch-boundary",
            attempt_id="attempt:patch-boundary", run_id="job:patch-boundary", goal_id="goal:patch-boundary", now=now)
        packet = await service.inspect_and_prepare(request, owner=owner, work_board_task_id="task:patch-boundary",
            work_board_attempt_id="attempt:patch-boundary", workflow_run_id="job:patch-boundary",
            goal_id="goal:patch-boundary", goal_revision=1, db=db)
        output["base_snapshot_sha256"] = packet.base_snapshot_sha256
        await db.commit()
        consent = await service.grant_egress_consent(owner=owner, work_board_task_id="task:patch-boundary",
            work_board_attempt_id="attempt:patch-boundary", workflow_run_id="job:patch-boundary", packet=packet,
            effective_profile_id="openrouter", effective_upstream="openrouter", request_key="request:patch-boundary",
            expires_at=now + timedelta(minutes=5), db=db)
        await db.commit()
        principal = TrustPrincipal(principal_id=owner.principal_id, principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,), session_id=owner.session_id, job_id="job:patch-boundary",
            operator_session_id=owner.session_id)
        with pytest.raises(RepoRepairError) as rejected:
            await service.generate_proposal(packet, request, owner=owner, principal=principal,
                lease_owner="worker:repo-repair", fencing_token=1, consent=consent, model=Model())
    assert rejected.value.code == "model_patch_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("section", _PROPOSAL_SECTIONS)
async def test_iterative_proposal_rejects_unsupported_patch_sections(accounting_db, monkeypatch,
        repository_admission_signer, section):
    import json
    import httpx
    from sqlalchemy import select
    from src.api.workflows import RepoRepairEgressConsentRequest
    from src.db.models import ApprovalRequest, RepoRepairProposal
    from src.workflows.repo_repair_source import grant_repository_iteration_consent, repository_source_preview
    from tests.test_repository_work_lane_release import _original
    flow = await _original(accounting_db, monkeypatch)
    service, jobs, owner, root_id = flow["service"], flow["jobs"], flow["owner"], flow["root_id"]
    preview = await repository_source_preview(service.repository_source_service, jobs, job_id=root_id, owner=owner)
    review, egress, packet = preview["repository_review"], preview["egress"], preview["source_packet"]
    source = json.loads(egress["request_body"]["messages"][1]["content"])
    output = {"summary": "Scripted change", "base_snapshot_sha256": source["source_packet"]["base_snapshot_sha256"],
        "patch_unified_diff": _model_patch("calculator.py", section),
        "allowed_paths": ["calculator.py", "tests/test_calculator.py"],
        "test_args": ["pytest", "-q", "tests/test_calculator.py"], "expected_outcome": "Checks run"}
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_client(*args, **kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "scripted-boundary",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": "0"},
            "choices": [{"message": {"role": "assistant", "content": json.dumps(output)}}]}))))
    with pytest.raises(Exception) as rejected:
        await grant_repository_iteration_consent(service.repository_source_service, jobs, job_id=root_id,
            owner=owner, request=RepoRepairEgressConsentRequest(expected_job_revision=preview["revision"],
                source_packet_digest=packet["artifact_sha256"],
                expected_source_manifest_digest=packet["source_manifest_sha256"],
                expected_profile_id=egress["effective_profile_id"], acknowledged_selected_source=True,
                idempotency_key="boundary-contact", expected_iteration_index=review["iteration_index"],
                expected_iteration_id=review["iteration_id"], expected_preparation_digest=review["preparation_digest"],
                expected_request_body_digest=egress["request_body_digest"],
                expected_request_route_digest=egress["request_route_digest"],
                expected_egress_envelope_digest=egress["egress_envelope_digest"],
                expected_diagnostics_digest=egress["diagnostics_digest"],
                expected_redaction_version=egress["redaction_version"], acknowledged_diagnostics=True),
            general_task_service=service, principal=flow["operator"].principal)
    assert type(rejected.value).__name__ == "RepoSandboxError"
    async with flow["factory"]() as db:
        # No approvable proposal or approval exists for the rejected diff.
        assert await db.get(RepoRepairProposal, "repository-proposal:" + review["iteration_id"]) is None
        assert list((await db.scalars(select(ApprovalRequest))).all()) == []


def test_supervised_publication_refuses_consistently_forged_stage_outputs(tmp_path):
    """The real supervisor child refuses a forged diff with a matching forged tree."""
    import hashlib
    from src.execution.repo_publication_supervisor import admit, guard, identity, run
    root, source, preview, approved = fixture_repo(tmp_path)
    (root / "app.py").write_text("VALUE = 2\n")
    (root / "conftest.py").write_text("print('persisted by a test')\n")
    git(root, "add", "-N", "conftest.py")
    forged = git(root, "diff", "--binary")
    forged_tree = file_manifest(root)
    (root / "conftest.py").unlink()
    (root / "app.py").write_text("VALUE = 1\n")
    git(root, "reset", "-q")
    forged_preview = {**preview, "tested_input": {**preview["tested_input"], "tested_files": forged_tree},
        "repair_binding": {"approved_input_patch_digest": hashlib.sha256(approved).hexdigest()},
        "local_posture": {"supervisor": identity()}}
    work = tmp_path / "publication"
    work.mkdir(mode=0o700)
    stage = work / "producer"
    with guard(stage) as (_, directory, guard_fd):
        with pytest.raises(ValueError, match="publication_approved_input_patch_changed"):
            admit(stage, source, forged_preview, forged, {"job_id": "forged"}, directory=directory,
                guard_fd=guard_fd, seconds_limit=20, approved_patch=forged)
        admission = admit(stage, source, forged_preview, forged, {"job_id": "forged"}, directory=directory,
            guard_fd=guard_fd, seconds_limit=20, approved_patch=approved)
        with pytest.raises(ValueError, match="tested_tree_differs_from_approved_input"):
            run(admission, lambda: None)
    assert not (stage / ".git/refs/heads/feat/tested-patch").exists()
