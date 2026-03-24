from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import settings
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.browser.sessions import browser_session_runtime
from src.tools.browser_session_tool import browser_session


@pytest.fixture(autouse=True)
def reset_browser_sessions():
    browser_session_runtime.reset_for_tests()
    yield
    browser_session_runtime.reset_for_tests()


@pytest.fixture
def browser_runtime_session():
    tokens = set_runtime_context("session-1", "high_risk")
    try:
        yield "session-1"
    finally:
        reset_runtime_context(tokens)


def test_browser_session_lists_empty_runtime(browser_runtime_session):
    assert browser_session(action="list") == "No browser sessions are open."


def test_browser_session_open_snapshot_read_and_close_round_trip(browser_runtime_session):
    with patch("src.tools.browser_session_tool.browse_webpage", side_effect=["first capture", "second capture"]):
        opened = browser_session(action="open", url="https://example.com/docs")
        assert "Opened browser session" in opened
        session_id = opened.split("session ")[1].split(" using")[0]
        latest_ref = opened.split("Latest ref: ")[1].split(".")[0]

        listed = browser_session(action="list")
        assert session_id in listed

        read_latest = browser_session(action="read", ref=latest_ref)
        assert "first capture" in read_latest

        snapshotted = browser_session(action="snapshot", session_id=session_id)
        assert f"session {session_id}" in snapshotted
        new_ref = snapshotted.split("snapshot ")[1].split(" for")[0]

        read_new = browser_session(action="read", ref=new_ref)
        assert "second capture" in read_new

        closed = browser_session(action="close", session_id=session_id)
        assert closed == f"Closed browser session {session_id}."


def test_browser_session_uses_configured_browser_provider_with_local_fallback(tmp_path, browser_runtime_session):
    workspace = tmp_path / "workspace"
    extension_dir = workspace / "extensions" / "browserbase-pack"
    (extension_dir / "connectors" / "browser").mkdir(parents=True)
    (extension_dir / "manifest.yaml").write_text(
        "id: seraph.browserbase-pack\n"
        "version: 2026.3.23\n"
        "display_name: Browserbase Pack\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  browser_providers:\n"
        "    - connectors/browser/browserbase.yaml\n"
        "permissions:\n"
        "  network: true\n",
        encoding="utf-8",
    )
    (extension_dir / "connectors" / "browser" / "browserbase.yaml").write_text(
        "name: browserbase\n"
        "description: Browserbase provider\n"
        "provider_kind: browserbase\n"
        "enabled: true\n"
        "config_fields:\n"
        "  - key: api_key\n"
        "    label: Browserbase API Key\n"
        "    input: password\n"
        "    required: true\n",
        encoding="utf-8",
    )
    (workspace / "extensions-state.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "seraph.browserbase-pack": {
                        "config": {
                            "browser_providers": {
                                "browserbase": {"api_key": "secret"},
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with (
        patch.object(settings, "workspace_dir", str(workspace)),
        patch("src.tools.browser_session_tool.browse_webpage", return_value="provider capture"),
    ):
        opened = browser_session(action="open", url="https://example.com", provider="browserbase")

    assert "browserbase (browserbase)" in opened
    assert "local browser runtime" in opened
    listed = browser_session(action="list")
    assert "mode=local_fallback" in listed
    latest_ref = opened.split("Latest ref: ")[1].split(".")[0]
    read_latest = browser_session(action="read", ref=latest_ref)
    assert "mode=local_fallback" in read_latest


def test_browser_session_errors_for_missing_requested_provider(tmp_path, browser_runtime_session):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch.object(settings, "workspace_dir", str(workspace)):
        result = browser_session(action="open", url="https://example.com", provider="browserbase")

    assert result == "Error: Browser provider 'browserbase' is not enabled and configured."


def test_browser_session_rejects_open_failure_without_persisting_session(browser_runtime_session):
    with patch("src.tools.browser_session_tool.browse_webpage", return_value="Error: Access to 'blocked.example' is blocked by site policy."):
        result = browser_session(action="open", url="https://blocked.example")

    assert result == "Error: Access to 'blocked.example' is blocked by site policy."
    assert browser_session(action="list") == "No browser sessions are open."


def test_browser_session_rejects_snapshot_failure_without_persisting_capture(browser_runtime_session):
    timeout_error = f"Error: browsing https://example.com/docs timed out after {settings.browser_timeout}s"
    with patch("src.tools.browser_session_tool.browse_webpage", side_effect=["first capture", timeout_error]):
        opened = browser_session(action="open", url="https://example.com/docs")
        session_id = opened.split("session ")[1].split(" using")[0]
        latest_ref = opened.split("Latest ref: ")[1].split(".")[0]

        failed = browser_session(action="snapshot", session_id=session_id)
        assert failed == timeout_error

        session_payload = browser_session_runtime.get_session(session_id, owner_session_id="session-1")
        assert session_payload is not None
        assert len(session_payload["snapshots"]) == 1
        assert session_payload["snapshots"][0]["ref"] == latest_ref


def test_browser_session_scopes_sessions_and_refs_to_current_session():
    first_tokens = set_runtime_context("session-1", "high_risk")
    try:
        with patch("src.tools.browser_session_tool.browse_webpage", return_value="first capture"):
            opened = browser_session(action="open", url="https://example.com/docs")
    finally:
        reset_runtime_context(first_tokens)

    session_id = opened.split("session ")[1].split(" using")[0]
    latest_ref = opened.split("Latest ref: ")[1].split(".")[0]

    second_tokens = set_runtime_context("session-2", "high_risk")
    try:
        assert browser_session(action="list") == "No browser sessions are open."
        assert browser_session(action="read", ref=latest_ref) == f"Error: Browser ref '{latest_ref}' was not found."
        assert browser_session(action="close", session_id=session_id) == f"Error: Browser session '{session_id}' was not found."
    finally:
        reset_runtime_context(second_tokens)


def test_browser_session_allows_captures_whose_text_starts_with_error_label(browser_runtime_session):
    with patch("src.tools.browser_session_tool.browse_webpage", return_value="Error budgets are rising in Q2"):
        opened = browser_session(action="open", url="https://example.com/report")

    assert "Opened browser session" in opened
    assert "Error budgets are rising in Q2" in opened
