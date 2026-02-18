"""Tests for the app blocklist module."""

import json
import os
import platform
import tempfile

import pytest

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="Daemon tests require macOS",
)

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from blocklist import DEFAULT_BLOCKLIST, is_blocked, load_blocklist


class TestDefaultBlocklist:
    def test_contains_password_managers(self):
        assert "1password" in DEFAULT_BLOCKLIST
        assert "bitwarden" in DEFAULT_BLOCKLIST
        assert "lastpass" in DEFAULT_BLOCKLIST

    def test_contains_banking(self):
        assert "chase" in DEFAULT_BLOCKLIST
        assert "wells fargo" in DEFAULT_BLOCKLIST

    def test_contains_crypto(self):
        assert "ledger live" in DEFAULT_BLOCKLIST
        assert "metamask" in DEFAULT_BLOCKLIST

    def test_all_lowercase(self):
        for app in DEFAULT_BLOCKLIST:
            assert app == app.lower(), f"Entry {app!r} should be lowercase"


class TestIsBlocked:
    def test_exact_match(self):
        blocklist = {"1password"}
        assert is_blocked("1password", blocklist)

    def test_case_insensitive(self):
        blocklist = {"1password"}
        assert is_blocked("1Password", blocklist)
        assert is_blocked("1PASSWORD", blocklist)

    def test_substring_match(self):
        blocklist = {"1password"}
        assert is_blocked("1Password 7", blocklist)
        assert is_blocked("1Password — Vault", blocklist)

    def test_not_blocked(self):
        blocklist = {"1password"}
        assert not is_blocked("VS Code", blocklist)
        assert not is_blocked("Safari", blocklist)

    def test_empty_blocklist(self):
        assert not is_blocked("1Password", set())

    def test_multi_word_entry(self):
        blocklist = {"bank of america"}
        assert is_blocked("Bank of America", blocklist)
        assert is_blocked("Bank Of America Online", blocklist)


class TestLoadBlocklist:
    def test_defaults_when_no_config(self):
        result = load_blocklist(None)
        assert result == DEFAULT_BLOCKLIST

    def test_defaults_when_file_not_found(self):
        result = load_blocklist("/nonexistent/path.json")
        assert result == DEFAULT_BLOCKLIST

    def test_adds_blocked_apps(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"blocked_apps": ["TikTok", "Instagram"]}, f)
            f.flush()
            try:
                result = load_blocklist(f.name)
                assert "tiktok" in result
                assert "instagram" in result
                # Defaults still present
                assert "1password" in result
            finally:
                os.unlink(f.name)

    def test_removes_allowed_apps(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"allowed_apps": ["Signal"]}, f)
            f.flush()
            try:
                result = load_blocklist(f.name)
                assert "signal" not in result
                # Other defaults still present
                assert "1password" in result
            finally:
                os.unlink(f.name)

    def test_add_and_remove(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "blocked_apps": ["MyBank"],
                "allowed_apps": ["Signal"],
            }, f)
            f.flush()
            try:
                result = load_blocklist(f.name)
                assert "mybank" in result
                assert "signal" not in result
            finally:
                os.unlink(f.name)

    def test_invalid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json")
            f.flush()
            try:
                result = load_blocklist(f.name)
                assert result == DEFAULT_BLOCKLIST
            finally:
                os.unlink(f.name)
