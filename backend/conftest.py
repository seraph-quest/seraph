import os
import shutil
import tempfile
import time

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
# Operator auth and screenshot analysis change the outcome of ordinary route
# and observer tests. A shell that sourced an operator env file (``manage.sh``
# sources it with ``set -a``) would export these, so pin them here instead of
# per test. Tests that need another value set it in their own monkeypatch scope.
# This list covers the variables known to change test outcomes; other settings
# variables in the process environment are the caller's explicit input (CI and
# shard runners set some on purpose) and still reach the tests.
os.environ["DEPLOYMENT_ENVIRONMENT"] = "test"
os.environ["OPERATOR_AUTH_ALLOW_UNAUTHENTICATED_TESTS"] = "true"
for _ambient in (
    "OPERATOR_AUTH_SECRET",
    "OPERATOR_AUTH_SECRET_HASH",
    "OPERATOR_AUTH_LOCAL_NO_LOGIN",
    "OPERATOR_AUTH_ALLOWED_HOSTS",
    "OPERATOR_AUTH_ALLOWED_ORIGINS",
    "SCREEN_ANALYSIS_PROVIDER",
    "LOCAL_VLM_BASE_URL",
):
    os.environ.pop(_ambient, None)


def pytest_unconfigure(config):
    if _SESSION_WORKSPACE is not None:
        shutil.rmtree(_SESSION_WORKSPACE, ignore_errors=True)
