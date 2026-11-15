"""Tests run on hermetic inputs; production keeps its env file (#1219)."""

import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from config import settings as settings_module
from config.settings import (
    DEFAULT_ENV_FILE,
    REPO_ROOT,
    SETTINGS_ENV_FILE_ENV,
    Settings,
    resolve_settings_env_file,
    settings,
)
import conftest as root_conftest
from tests.conftest import TABLE_MODEL_MODULES, restore_settings, snapshot_settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _staged_repo(tmp_path: Path) -> Path:
    """Copy the settings package into a throwaway repository root.

    The child imports this copy, so its ``REPO_ROOT`` is ``tmp_path / "repo"``
    and a checkout's real ``.env.dev`` is never read or parsed by these tests.
    """
    config_dir = tmp_path / "repo" / "backend" / "config"
    config_dir.mkdir(parents=True)
    for name in ("__init__.py", "settings.py"):
        (config_dir / name).write_bytes((BACKEND_ROOT / "config" / name).read_bytes())
    return tmp_path / "repo"


def _settings_in_fresh_process(repo: Path, env: dict[str, str]) -> dict:
    """Import the staged ``config.settings`` in a new interpreter."""
    script = (
        "import json\n"
        "from config.settings import REPO_ROOT, Settings, settings\n"
        "env_file = Settings.model_config['env_file']\n"
        "print(json.dumps({'repo_root': str(REPO_ROOT),\n"
        "                  'env_file': None if env_file is None else str(env_file),\n"
        "                  'local_no_login': settings.operator_auth_local_no_login}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo / "backend",
        env={**env, "PYTHONPATH": str(repo / "backend")},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _launcher_env() -> dict[str, str]:
    # The launchers' view: no selector, and no settings variables exported.
    return {
        key: value
        for key, value in os.environ.items()
        if key not in {SETTINGS_ENV_FILE_ENV, "OPERATOR_AUTH_LOCAL_NO_LOGIN", "PYTHONPATH"}
    }


def test_unselected_process_loads_the_repository_dev_env_file(tmp_path):
    # ``manage.sh`` starts the backend without the selector, so the backend
    # still loads ``REPO_ROOT/.env.dev`` exactly as before the seam existed.
    assert DEFAULT_ENV_FILE == REPO_ROOT / ".env.dev"
    assert resolve_settings_env_file({}) == DEFAULT_ENV_FILE
    repo = _staged_repo(tmp_path)
    (repo / ".env.dev").write_text("OPERATOR_AUTH_LOCAL_NO_LOGIN=true\n", encoding="utf-8")
    child = _settings_in_fresh_process(repo, _launcher_env())
    assert child == {
        "repo_root": str(repo.resolve()),
        "env_file": str(repo.resolve() / ".env.dev"),
        "local_no_login": True,
    }


def test_selector_names_a_file_or_none(tmp_path):
    chosen = tmp_path / "chosen.env"
    assert resolve_settings_env_file({SETTINGS_ENV_FILE_ENV: str(chosen)}) == chosen
    assert resolve_settings_env_file({SETTINGS_ENV_FILE_ENV: ""}) is None

    repo = _staged_repo(tmp_path)
    (repo / ".env.dev").write_text("OPERATOR_AUTH_LOCAL_NO_LOGIN=true\n", encoding="utf-8")
    chosen.write_text("DEBUG=false\n", encoding="utf-8")
    env = _launcher_env()
    env[SETTINGS_ENV_FILE_ENV] = str(chosen)
    child = _settings_in_fresh_process(repo, env)
    assert (child["env_file"], child["local_no_login"]) == (str(chosen), False)
    env[SETTINGS_ENV_FILE_ENV] = ""
    child = _settings_in_fresh_process(repo, env)
    assert (child["env_file"], child["local_no_login"]) == (None, False)


def test_selected_env_file_values_are_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("OPERATOR_AUTH_LOCAL_NO_LOGIN", raising=False)
    chosen = tmp_path / "chosen.env"
    chosen.write_text("OPERATOR_AUTH_LOCAL_NO_LOGIN=true\n", encoding="utf-8")
    loaded = Settings(_env_file=resolve_settings_env_file({SETTINGS_ENV_FILE_ENV: str(chosen)}))
    assert loaded.operator_auth_local_no_login is True
    unloaded = Settings(_env_file=resolve_settings_env_file({SETTINGS_ENV_FILE_ENV: ""}))
    assert unloaded.operator_auth_local_no_login is False


def test_launchers_do_not_set_the_selector():
    # The managed launcher and both compose topologies keep their own env
    # files and never select a different settings file.
    repo = REPO_ROOT
    for name in ("manage.sh", "docker-compose.dev.yaml", "docker-compose.prod.yaml",
                 "env.dev.example", "env.prod.example"):
        assert SETTINGS_ENV_FILE_ENV not in (repo / name).read_text(encoding="utf-8"), name
    assert ".env.dev" in (repo / "docker-compose.dev.yaml").read_text(encoding="utf-8")
    assert ".env.prod" in (repo / "docker-compose.prod.yaml").read_text(encoding="utf-8")
    assert 'ENV_FILE="$SCRIPT_DIR/.env.$ENV"' in (repo / "manage.sh").read_text(encoding="utf-8")


def test_test_process_loads_no_env_file_and_runs_in_utc():
    assert os.environ[SETTINGS_ENV_FILE_ENV] == ""
    assert Settings.model_config["env_file"] is None
    assert settings_module.Settings is Settings
    assert os.environ["TZ"] == "UTC"
    assert time.strftime("%Z", time.localtime(0)) in {"UTC", "GMT"}
    assert time.timezone == 0
    # No ambient settings variable reaches a test, beyond the allowlist and
    # the values the root conftest sets itself.
    assert os.environ["DEPLOYMENT_ENVIRONMENT"] == "test"
    leftover = set(root_conftest.ambient_settings_variables(os.environ, frozenset(Settings.model_fields)))
    assert leftover <= {"OPENROUTER_API_KEY", "SCHEDULER_ENABLED", "DEPLOYMENT_ENVIRONMENT",
                        "OPERATOR_AUTH_ALLOW_UNAUTHENTICATED_TESTS", "CAPABILITY_JOURNAL_SECRET"}
    assert settings.operator_auth_local_no_login is False
    assert settings.operator_auth_secret == ""
    assert settings.screen_analysis_provider == ""


def test_ambient_settings_strip_covers_every_field_except_the_allowlist():
    # The ast reading of the source names exactly the pydantic fields.
    assert root_conftest.settings_field_names() == frozenset(Settings.model_fields)
    environ = {
        "OPERATOR_AUTH_LOCAL_NO_LOGIN": "true",
        "debug": "true",
        "REPO_SANDBOX__MAX_FILES": "1",
        "WORKSPACE_DIR": "/caller/workspace",
        "PATH": "/bin",
        "SERAPH_TEST_OWNED_WORKSPACE_DIR": "/x",
    }
    stripped = root_conftest.ambient_settings_variables(environ, frozenset(Settings.model_fields))
    assert sorted(stripped) == ["OPERATOR_AUTH_LOCAL_NO_LOGIN", "REPO_SANDBOX__MAX_FILES", "debug"]


_LEAK_SENTINEL = 987.5


@pytest.mark.parametrize("phase", ["leak", "observe"])
def test_direct_settings_assignment_does_not_leak(phase):
    # Parametrized cases run in order: the first assigns without cleanup,
    # the second must see the pre-test value restored.
    if phase == "leak":
        assert settings.agent_chat_timeout != _LEAK_SENTINEL
        settings.agent_chat_timeout = _LEAK_SENTINEL
        settings.repo_sandbox.max_files = -1
    else:
        assert settings.agent_chat_timeout != _LEAK_SENTINEL
        assert settings.repo_sandbox.max_files != -1


def test_restore_undoes_direct_and_nested_settings_changes():
    # Order-independent check of the helpers the teardown hook calls.
    before_timeout = settings.agent_chat_timeout
    before_max_files = settings.repo_sandbox.max_files
    snapshot = snapshot_settings()
    settings.agent_chat_timeout = _LEAK_SENTINEL
    settings.repo_sandbox.max_files = -1
    settings.__dict__["not_a_field"] = 1
    restore_settings(snapshot)
    assert settings.agent_chat_timeout == before_timeout
    assert settings.repo_sandbox.max_files == before_max_files
    assert "not_a_field" not in settings.__dict__


def _table_model_modules_in_src() -> set[str]:
    owners: set[str] = set()
    for path in sorted((BACKEND_ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                keyword.arg == "table"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                relative = path.relative_to(BACKEND_ROOT).with_suffix("")
                owners.add(".".join(relative.parts))
                break
    return owners


def test_conftest_registers_every_table_model_before_create_all():
    assert set(TABLE_MODEL_MODULES) == _table_model_modules_in_src()
    assert all(module in sys.modules for module in TABLE_MODEL_MODULES)
