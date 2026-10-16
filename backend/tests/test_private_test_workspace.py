"""The default pytest workspace is private to each session (#1097)."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from config.settings import settings

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_PROBE_ENV = "SERAPH_TEST_WORKSPACE_PROBE"
_OWNED_WORKSPACE_ENV = "SERAPH_TEST_OWNED_WORKSPACE_DIR"


def test_default_workspace_is_private_to_this_session():
    workspace = Path(os.environ["WORKSPACE_DIR"])
    if os.environ.get(_OWNED_WORKSPACE_ENV) != str(workspace):
        pytest.skip("WORKSPACE_DIR was supplied explicitly by the caller")

    assert workspace != Path("/tmp/seraph-test")
    assert workspace.parent == Path(tempfile.gettempdir())
    assert workspace.name.startswith("seraph-test-")
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
    assert workspace.stat().st_uid == os.getuid()
    assert Path(settings.workspace_dir) == workspace


@pytest.mark.skipif(not os.environ.get(_PROBE_ENV), reason="spawned by the isolation driver only")
def test_workspace_isolation_probe():
    probe = Path(os.environ[_PROBE_ENV])
    workspace = Path(os.environ["WORKSPACE_DIR"])
    (workspace / f"sentinel-{probe.name}").write_text("probe\n", encoding="utf-8")
    (workspace / ".vault-key").write_text(f"not-a-key-{probe.name}\n", encoding="utf-8")
    probe.with_suffix(".ready").write_text("ready\n", encoding="utf-8")

    # Hold until every probe has written into its workspace, so the listing
    # below runs while the other session is alive and populated.
    deadline = time.monotonic() + 60
    while len(list(probe.parent.glob("*.ready"))) < 2:
        assert time.monotonic() < deadline, "peer probe never became ready"
        time.sleep(0.05)

    probe.with_suffix(".json").write_text(
        json.dumps(
            {
                "workspace": str(workspace),
                "mode": stat.S_IMODE(workspace.stat().st_mode),
                "entries": sorted(path.name for path in workspace.iterdir()),
                "vault_key": (workspace / ".vault-key").read_text(encoding="utf-8"),
            }
        ),
        encoding="utf-8",
    )


def _probe_env(probe: Path, **extra: str) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"WORKSPACE_DIR", _OWNED_WORKSPACE_ENV, "PYTEST_XDIST_WORKER", "PYTEST_CURRENT_TEST"}
    }
    env[_PROBE_ENV] = str(probe)
    env.update(extra)
    return env


def _spawn_probe(probe: Path, **extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "-q",
            f"{Path(__file__).relative_to(_BACKEND_ROOT)}::test_workspace_isolation_probe",
        ],
        cwd=_BACKEND_ROOT,
        env=_probe_env(probe, **extra),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait(process: subprocess.Popen) -> None:
    output, _ = process.communicate(timeout=180)
    assert process.returncode == 0, output


def test_concurrent_sessions_cannot_observe_each_other(tmp_path):
    parent_workspace = Path(os.environ["WORKSPACE_DIR"])
    probes = [tmp_path / "alpha", tmp_path / "beta"]
    processes = [_spawn_probe(probe) for probe in probes]
    for process in processes:
        _wait(process)

    alpha, beta = (json.loads(probe.with_suffix(".json").read_text(encoding="utf-8")) for probe in probes)
    assert alpha["workspace"] != beta["workspace"]
    assert alpha["workspace"] != str(parent_workspace)
    assert alpha["mode"] == beta["mode"] == 0o700
    assert "sentinel-alpha" in alpha["entries"] and "sentinel-beta" not in alpha["entries"]
    assert "sentinel-beta" in beta["entries"] and "sentinel-alpha" not in beta["entries"]
    assert alpha["vault_key"] == "not-a-key-alpha\n"
    assert beta["vault_key"] == "not-a-key-beta\n"
    # Each session removes its own workspace and leaves the parent's alone.
    assert not Path(alpha["workspace"]).exists()
    assert not Path(beta["workspace"]).exists()
    assert parent_workspace.is_dir()


def test_explicit_workspace_dir_is_honoured_and_kept(tmp_path):
    explicit = tmp_path / "explicit-workspace"
    explicit.mkdir(mode=0o700)
    probe = tmp_path / "solo"
    # A single probe needs a second ready marker to pass the barrier.
    (tmp_path / "peer.ready").write_text("ready\n", encoding="utf-8")

    _wait(_spawn_probe(probe, WORKSPACE_DIR=str(explicit)))

    report = json.loads(probe.with_suffix(".json").read_text(encoding="utf-8"))
    assert report["workspace"] == str(explicit)
    assert (explicit / "sentinel-solo").exists()
