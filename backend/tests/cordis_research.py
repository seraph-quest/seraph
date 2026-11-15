"""Shared real-child Cordis research host for guardian discovery tests (#1008).

Discovery planning and source extraction run only through the reviewed Cordis
child; there is no Python fallback, so these suites need the built package and
``SERAPH_CORDIS_TEST_NODE``. The child gets no PATH/HOME/credentials and speaks
only over owned stdio pipes.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import inspect
import json
import os
import shutil
from pathlib import Path

import pytest

NODE = os.environ.get("SERAPH_CORDIS_TEST_NODE")
PACKAGE = Path(__file__).resolve().parents[2] / "runtime" / "cordis"
needs_cordis = pytest.mark.skipif(
    not NODE or not Path(NODE).is_file() or not (PACKAGE / "node_modules/cordis").is_dir()
    or not (PACKAGE / "dist/build-manifest.json").is_file(),
    reason="reviewed Cordis test node/package not built (set SERAPH_CORDIS_TEST_NODE)")


async def seed_composition_inventory():
    """Fresh test databases get the same legacy owner rows as production startup."""
    from src.db.engine import get_session
    from src.runtime_plugins.ownership import seed_legacy_inventory_sync
    async with get_session() as db:
        await db.run_sync(lambda session: seed_legacy_inventory_sync(session.connection()))


@asynccontextmanager
async def research_host(jobs, *, package_root: Path | None = None, observe=None):
    """Start one reviewed child whose callbacks reach the canonical Python owner."""
    from src.runtime_plugins.bridge import CordisHost
    from src.runtime_plugins.dispatch import NativeServiceDispatcher
    await seed_composition_inventory()
    dispatcher = NativeServiceDispatcher(jobs=jobs)
    if observe is not None:
        original = dispatcher.dispatch

        async def observed(frame, **kwargs):
            pending = observe(frame)
            if inspect.isawaitable(pending):
                await pending
            return await original(frame, **kwargs)
        dispatcher.dispatch = observed
    host = CordisHost(node_path=Path(NODE), service_dispatch=dispatcher, package_root=package_root)
    try:
        assert await host.start(), host.snapshot()
        yield host
    finally:
        await host.stop()
        assert host.snapshot()["cleanup"]["process_reaped"] is True


def package_variant(directory: Path, mutate) -> Path:
    """A separately reviewed package copy whose profile differs only by ``mutate``.

    The build-manifest receipt is recomputed exactly as scripts/build-manifest.mjs
    does, so the copy passes the same package/profile/plugin-digest checks.
    """
    from src.runtime_plugins.composition import BUILD_FILES, NPM_VERSION
    root = directory.resolve() / "cordis"
    shutil.copytree(PACKAGE, root, ignore=shutil.ignore_patterns("typescript", "@types", "undici-types", ".build-home"))
    profile = json.loads((root / "profile.json").read_text())
    mutate(profile)
    (root / "profile.json").write_text(json.dumps(profile, indent=2) + "\n")
    files = {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in BUILD_FILES}
    (root / "dist/build-manifest.json").write_text(json.dumps({"format": 1, "npm_version": NPM_VERSION, "files": files}) + "\n")
    return root
