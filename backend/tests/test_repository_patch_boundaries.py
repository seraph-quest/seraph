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
