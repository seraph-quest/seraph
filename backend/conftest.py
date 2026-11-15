import ast
import os
import shutil
import tempfile
import time
from pathlib import Path

# Hermetic inputs (#1219). This root conftest runs before any test module or
# ``config.settings`` is imported, so these hold for the settings singleton.
# Load no dotenv file: the repository's ``.env.dev`` is the operator's live
# configuration and exists only in some checkouts. Set unconditionally, so an
# inherited value cannot select a file either; tests that need a specific
# file construct ``Settings(_env_file=...)`` themselves.
os.environ["SERAPH_SETTINGS_ENV_FILE"] = ""
# Production derives behaviour from the local hour and date. Pin the zone so
# a run's result does not depend on the host's timezone setting.
os.environ["TZ"] = "UTC"
time.tzset()

# Settings fields read from the process environment as well. A shell that
# sourced an operator env file (``manage.sh`` sources it with ``set -a``) would
# export them, so remove every variable named after a ``Settings`` field
# (matched case-insensitively, nested ``FIELD__KEY`` included) except
# ``AMBIENT_SETTINGS_ALLOWED``. The names are read from the source with ``ast``
# because importing ``config.settings`` would build the singleton too early.
# Tests that need a value set it themselves, in their own monkeypatch scope.
SETTINGS_SOURCE = Path(__file__).resolve().parent / "config" / "settings.py"
# WORKSPACE_DIR is the caller's explicit test workspace (see below). CI sets
# only OPENROUTER_API_KEY, to the same value this conftest defaults it to.
AMBIENT_SETTINGS_ALLOWED = frozenset({"WORKSPACE_DIR"})


def settings_field_names(source: Path = SETTINGS_SOURCE) -> frozenset[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            return frozenset(
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and item.target.id != "model_config"
            )
    raise RuntimeError(f"no Settings class in {source}")


def ambient_settings_variables(environ, fields: frozenset[str]) -> list[str]:
    names = []
    for name in environ:
        if name.upper() in AMBIENT_SETTINGS_ALLOWED:
            continue
        lowered = name.lower()
        if lowered in fields or lowered.split("__", 1)[0] in fields:
            names.append(name)
    return names


for _ambient in ambient_settings_variables(os.environ, settings_field_names()):
    del os.environ[_ambient]

# Marks a WORKSPACE_DIR this conftest created, so it can be replaced per
# xdist worker and removed at session end. An explicit WORKSPACE_DIR from the
# environment is the caller's to manage and is left untouched.
_OWNED_WORKSPACE_ENV = "SERAPH_TEST_OWNED_WORKSPACE_DIR"


def _owns_workspace() -> bool:
    owned = os.environ.get(_OWNED_WORKSPACE_ENV)
    return bool(owned) and os.environ.get("WORKSPACE_DIR") == owned


def _create_private_workspace() -> str:
    # mkdtemp creates a unique 0700 directory, so concurrent pytest sessions
    # (parallel worktrees, local shards, repeated runs) never share state.
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    workspace = tempfile.mkdtemp(prefix=f"seraph-test-{worker + '-' if worker else ''}")
    os.environ["WORKSPACE_DIR"] = workspace
    os.environ[_OWNED_WORKSPACE_ENV] = workspace
    return workspace


# Only the process that created the workspace removes it: a nested pytest run
# started by a test inherits the marker but must not delete its parent's root.
_SESSION_WORKSPACE: str | None = None
if not os.environ.get("WORKSPACE_DIR") or (
    # An xdist worker inherits the controller's environment; give it its own.
    _owns_workspace() and os.environ.get("PYTEST_XDIST_WORKER")
):
    _SESSION_WORKSPACE = _create_private_workspace()

os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
# Ordinary route tests run with the test-only auth bypass. Tests that need
# another value set it in their own monkeypatch scope.
os.environ["DEPLOYMENT_ENVIRONMENT"] = "test"
os.environ["OPERATOR_AUTH_ALLOW_UNAUTHENTICATED_TESTS"] = "true"


def pytest_unconfigure(config):
    if _SESSION_WORKSPACE is not None:
        shutil.rmtree(_SESSION_WORKSPACE, ignore_errors=True)
