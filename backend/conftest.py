import os
import shutil
import tempfile

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
os.environ.setdefault("DEPLOYMENT_ENVIRONMENT", "test")
os.environ.setdefault("OPERATOR_AUTH_ALLOW_UNAUTHENTICATED_TESTS", "true")


def pytest_unconfigure(config):
    if _SESSION_WORKSPACE is not None:
        shutil.rmtree(_SESSION_WORKSPACE, ignore_errors=True)
