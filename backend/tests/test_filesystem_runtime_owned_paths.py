"""Agent filesystem tools refuse registry-owned runtime state (#1093)."""

import json
from unittest.mock import patch

import pytest

from src.tools import filesystem_tool
from src.tools.filesystem_tool import (
    RuntimeOwnedWorkspacePathError,
    _assert_agent_workspace_access,
    apply_workspace_patch,
    preview_workspace_patch,
    read_file,
    write_file,
)
from src.workspace import AgentWorkspaceAccess

_ORIGINAL = "runtime-owned before\n"
_OPERATIONS = ("read", "write", "preview", "apply")
# Registry paths that no agent file tool may read or write.
_DENIED_PATHS = (
    ".vault-key",
    "google_calendar_token.json",
    "seraph.db",
    "seraph.db-wal",
    "seraph.db-shm",
    "seraph.db-journal",
    "mcp-servers.json",
    "stdio-proxies.json",
    "artifacts/mail/private/message.json",
)
# Runtime-owned registry paths agents may inspect but not edit.
_READ_ONLY_PATHS = (
    "model-fabric-settings.json",
    "screen-analysis-settings.json",
    "extensions-state.json",
    "starter-packs.json",
    "extensions/pack/manifest.yaml",
    "daemon-status.json",
    "local-runtime-profile-receipts/receipt.json",
    ".seraph-extension-snapshots/pack.json",
    "cache/entry.txt",
)
_ORDINARY_PATHS = (
    "notes/today.md",
    "plans/q4.md",
    "reports/weekly.md",
    "runbooks/deploy.md",
    "skills/summarise.md",
    "workflows/triage.md",
    "artifacts/exports/summary.txt",
    "soul.md",
    "tmp/scratch.txt",
    "projects/app/src/main.py",
    "README.md",
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def without_name_lists(monkeypatch):
    """Disable the name-based defence so only the registry can refuse."""
    monkeypatch.setattr(filesystem_tool, "_is_secret_like_workspace_path", lambda _path: False)


def _seed(workspace, logical_path, content=_ORIGINAL):
    target = workspace / logical_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _run(operation, file_path):
    if operation == "read":
        return read_file.forward(file_path)
    if operation == "write":
        return write_file.forward(file_path, "agent overwrite\n")
    if operation == "preview":
        return preview_workspace_patch.forward(file_path, "before", "after")
    return apply_workspace_patch.forward(file_path, "before", "after")


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("logical_path", _DENIED_PATHS)
def test_registry_denies_secret_and_database_paths(workspace, without_name_lists, logical_path, operation):
    target = _seed(workspace, logical_path)

    with pytest.raises(RuntimeOwnedWorkspacePathError) as excinfo:
        _run(operation, logical_path)

    assert excinfo.value.reason_code == "workspace_path_runtime_owned"
    assert excinfo.value.access is AgentWorkspaceAccess.DENIED
    assert "not available to agents" in str(excinfo.value)
    assert target.read_text(encoding="utf-8") == _ORIGINAL


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("logical_path", _DENIED_PATHS)
def test_denied_paths_are_refused_with_name_lists_enabled(workspace, logical_path, operation):
    target = _seed(workspace, logical_path)

    with pytest.raises(ValueError, match="workspace path blocked"):
        _run(operation, logical_path)

    assert target.read_text(encoding="utf-8") == _ORIGINAL


@pytest.mark.parametrize("logical_path", (".vault-key", "google_calendar_token.json"))
def test_name_lists_cover_registry_secrets_as_defence_in_depth(workspace, logical_path):
    _seed(workspace, logical_path)

    with pytest.raises(ValueError, match="Secret-like workspace path blocked"):
        read_file.forward(logical_path)


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize(
    ("spelling", "logical_path"),
    (
        ("./seraph.db", "seraph.db"),
        ("notes/../seraph.db", "seraph.db"),
        ("notes//../seraph.db-wal", "seraph.db-wal"),
        ("notes/./../../{root}/.vault-key", ".vault-key"),
        ("plans/../notes/../google_calendar_token.json", "google_calendar_token.json"),
        ("{workspace}/seraph.db", "seraph.db"),
        ("{workspace}/notes/../.vault-key", ".vault-key"),
        ("seraph.db/.", "seraph.db"),
    ),
)
def test_traversal_and_absolute_spellings_are_classified_after_resolution(
    workspace, without_name_lists, spelling, logical_path, operation
):
    (workspace / "notes").mkdir(exist_ok=True)
    (workspace / "plans").mkdir(exist_ok=True)
    target = _seed(workspace, logical_path)
    file_path = spelling.format(workspace=workspace, root=workspace.name)

    with pytest.raises(RuntimeOwnedWorkspacePathError):
        _run(operation, file_path)

    assert target.read_text(encoding="utf-8") == _ORIGINAL


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("logical_path", (".vault-key", "seraph.db", "mcp-servers.json"))
def test_symlinks_to_protected_paths_are_refused(workspace, without_name_lists, logical_path, operation):
    target = _seed(workspace, logical_path)
    (workspace / "notes").mkdir()
    (workspace / "notes" / "alias.md").symlink_to(target)

    with pytest.raises(ValueError, match="blocked"):
        _run(operation, "notes/alias.md")

    assert target.read_text(encoding="utf-8") == _ORIGINAL


@pytest.mark.parametrize("logical_path", (".vault-key", "seraph.db", "mcp-servers.json"))
def test_registry_judges_the_symlink_target_not_the_alias(workspace, without_name_lists, logical_path):
    target = _seed(workspace, logical_path)
    (workspace / "notes").mkdir()
    alias = workspace / "notes" / "alias.md"
    alias.symlink_to(target)

    # ``_safe_resolve`` blocks symlinks first; the registry check must still
    # classify the resolved file if that boundary ever changes.
    with pytest.raises(RuntimeOwnedWorkspacePathError):
        _assert_agent_workspace_access("notes/alias.md", alias.resolve(), "read", write=False)


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize(
    "spelling",
    (".VAULT-KEY", ".Vault-Key", "SERAPH.DB", "Seraph.db-WAL", "seraph.DB-shm", "MCP-Servers.json", "Google_Calendar_Token.JSON"),
)
def test_case_variants_of_protected_paths_are_refused(workspace, without_name_lists, spelling, operation):
    # On a case-insensitive filesystem these spellings open the protected file.
    target = _seed(workspace, spelling)

    with pytest.raises(RuntimeOwnedWorkspacePathError):
        _run(operation, spelling)

    assert target.read_text(encoding="utf-8") == _ORIGINAL


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("logical_path", (".vault-key", "seraph.db", "mcp-servers.json", "model-fabric-settings.json"))
def test_hard_link_aliases_are_matched_by_file_identity(workspace, without_name_lists, logical_path, operation):
    # A hard link, like a case-insensitive filesystem alias, opens the
    # protected file under a name the registry does not declare.
    target = _seed(workspace, logical_path)
    (workspace / "notes").mkdir()
    alias = workspace / "notes" / "alias.md"
    alias.hardlink_to(target)

    with pytest.raises(RuntimeOwnedWorkspacePathError):
        if operation == "read" and logical_path == "model-fabric-settings.json":
            # Read-only state stays readable through any alias; edits do not.
            _run("write", "notes/alias.md")
        else:
            _run(operation, "notes/alias.md")

    assert target.read_text(encoding="utf-8") == _ORIGINAL


def test_directory_aliases_are_matched_by_file_identity(workspace, monkeypatch, without_name_lists):
    # Simulate a case-insensitive filesystem: ``Extensions`` resolves to the
    # same directory inode as the read-only ``extensions`` root.
    (workspace / "extensions").mkdir()
    (workspace / "Projects").mkdir()
    real_lstat = type(workspace).lstat
    extensions_stat = (workspace / "extensions").lstat()

    def aliased_lstat(path, *args, **kwargs):
        if path == workspace / "Projects":
            return extensions_stat
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(type(workspace), "lstat", aliased_lstat)

    with pytest.raises(RuntimeOwnedWorkspacePathError) as excinfo:
        write_file.forward("Projects/manifest.yaml", "id: planted\n")

    assert excinfo.value.access is AgentWorkspaceAccess.READ_ONLY
    assert not (workspace / "Projects" / "manifest.yaml").exists()


@pytest.mark.parametrize("spelling", (".vault-\u212aey", "google_calendar_to\u212aen.json", ".VAULT-\u212aEY"))
def test_unicode_equivalent_spellings_are_folded_before_classification(workspace, without_name_lists, spelling):
    # U+212A KELVIN SIGN normalises (NFC) to ASCII "K", then casefolds to "k".
    _seed(workspace, spelling)

    with pytest.raises(RuntimeOwnedWorkspacePathError):
        read_file.forward(spelling)


def test_mail_private_artifacts_are_denied_but_sibling_artifacts_are_not(workspace):
    target = _seed(workspace, "artifacts/mail/private/message.json")

    with pytest.raises(RuntimeOwnedWorkspacePathError):
        read_file.forward("artifacts/mail/private/message.json")
    with pytest.raises(RuntimeOwnedWorkspacePathError):
        write_file.forward("artifacts/mail/private/new.json", "{}")

    assert target.read_text(encoding="utf-8") == _ORIGINAL
    assert "Successfully wrote" in write_file.forward("artifacts/mail/summary.md", "ok\n")


@pytest.mark.parametrize("logical_path", _READ_ONLY_PATHS)
def test_read_only_runtime_paths_allow_read_but_refuse_edits(workspace, without_name_lists, logical_path):
    target = _seed(workspace, logical_path, "before\n")

    assert read_file.forward(logical_path) == "before\n"
    for operation in ("write", "preview", "apply"):
        with pytest.raises(RuntimeOwnedWorkspacePathError) as excinfo:
            _run(operation, logical_path)
        assert excinfo.value.access is AgentWorkspaceAccess.READ_ONLY
        assert "read-only for agents" in str(excinfo.value)

    assert target.read_text(encoding="utf-8") == "before\n"


def test_read_only_runtime_paths_refuse_new_files(workspace):
    with pytest.raises(RuntimeOwnedWorkspacePathError):
        write_file.forward("extensions/planted/manifest.yaml", "id: planted\n")

    assert not (workspace / "extensions" / "planted").exists()


@pytest.mark.parametrize("logical_path", _ORDINARY_PATHS)
def test_ordinary_workspace_files_remain_readable_and_writable(workspace, logical_path):
    assert "Successfully wrote" in write_file.forward(logical_path, "before\n")
    assert read_file.forward(logical_path) == "before\n"

    preview = json.loads(preview_workspace_patch.forward(logical_path, "before", "after"))
    assert preview["applied"] is False
    applied = json.loads(apply_workspace_patch.forward(logical_path, "before", "after"))
    assert applied["applied"] is True
    assert (workspace / logical_path).read_text(encoding="utf-8") == "after\n"


def test_lookalike_names_are_not_mistaken_for_registry_state(workspace):
    for logical_path in ("seraph.dbx", "notes/seraph.db", "projects/cache/out.txt", "extensions.md"):
        assert "Successfully wrote" in write_file.forward(logical_path, "ok\n")
        assert read_file.forward(logical_path) == "ok\n"


def test_runtime_owned_refusal_emits_blocked_receipt(workspace, without_name_lists):
    _seed(workspace, "seraph.db")

    with patch("src.tools.filesystem_tool.log_integration_event_sync") as log_event:
        with pytest.raises(RuntimeOwnedWorkspacePathError):
            read_file.forward("seraph.db")

    log_event.assert_called_once()
    kwargs = log_event.call_args.kwargs
    assert kwargs["outcome"] == "blocked"
    assert kwargs["details"]["operation"] == "read"
    assert "Runtime-owned workspace path blocked for read: seraph.db" in kwargs["details"]["error"]
