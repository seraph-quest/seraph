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
from tests.conftest import TABLE_MODEL_MODULES

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _settings_env_file_in_fresh_process(env: dict[str, str]) -> str | None:
    """Import ``config.settings`` in a new interpreter and report its env file."""
    script = (
        "import json\n"
        "from config.settings import Settings\n"
        "env_file = Settings.model_config['env_file']\n"
        "print(json.dumps(None if env_file is None else str(env_file)))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _launcher_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != SETTINGS_ENV_FILE_ENV}
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    return env


def test_unselected_process_loads_the_repository_dev_env_file():
    # ``manage.sh`` starts the backend without the selector, so the backend
    # still loads ``REPO_ROOT/.env.dev`` exactly as before the seam existed.
    assert DEFAULT_ENV_FILE == REPO_ROOT / ".env.dev"
    assert resolve_settings_env_file({}) == DEFAULT_ENV_FILE
    assert _settings_env_file_in_fresh_process(_launcher_env()) == str(REPO_ROOT / ".env.dev")


def test_selector_names_a_file_or_none(tmp_path):
    chosen = tmp_path / "chosen.env"
    assert resolve_settings_env_file({SETTINGS_ENV_FILE_ENV: str(chosen)}) == chosen
    assert resolve_settings_env_file({SETTINGS_ENV_FILE_ENV: ""}) is None

    env = _launcher_env()
    env[SETTINGS_ENV_FILE_ENV] = str(chosen)
    assert _settings_env_file_in_fresh_process(env) == str(chosen)
    env[SETTINGS_ENV_FILE_ENV] = ""
    assert _settings_env_file_in_fresh_process(env) is None


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
    # Ambient operator auth and screenshot-analysis variables never reach a test.
    assert os.environ["DEPLOYMENT_ENVIRONMENT"] == "test"
    for name in ("OPERATOR_AUTH_SECRET", "OPERATOR_AUTH_LOCAL_NO_LOGIN",
                 "SCREEN_ANALYSIS_PROVIDER", "LOCAL_VLM_BASE_URL"):
        assert name not in os.environ, name
    assert settings.operator_auth_local_no_login is False
    assert settings.operator_auth_secret == ""
    assert settings.screen_analysis_provider == ""


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
