"""The source inventory guard (#1205, design #1026 §5).

Every lifecycle shape the AST scan finds in ``backend/src`` must be a row of
``runtime_plugins.inventory``: an owner with a real domain and its A3.3-1
bundle, a listed gap with tracked work, or a reasoned exclusion.
"""
from __future__ import annotations

from pathlib import Path
import textwrap

import pytest

from src.runtime_plugins import inventory
from src.runtime_plugins.lifecycle import job_domain
from src.runtime_plugins.ownership import DOMAINS
from src.runtime_plugins.profiles import BUNDLE_NAMES
from src.runtime_plugins.trust import DOMAIN_BUNDLES, TRUST_BUNDLE


@pytest.fixture(scope="module")
def scan() -> frozenset[tuple[str, str]]:
    """One AST pass over backend/src, shared by the tests in this file."""
    return frozenset(inventory.scan_lifecycle_shapes())


def test_every_lifecycle_shape_in_src_is_mapped_gapped_or_excluded(scan):
    missing = inventory.unaccounted(set(scan))
    assert not missing, (
        "runtime owners in backend/src with no row in src/runtime_plugins/inventory.py; add each to OWNERS "
        f"(an existing domain), GAPS (with its tracked slice) or EXCLUSIONS (with a reason): {missing}")


def test_no_inventory_row_is_stale(scan):
    gone = inventory.stale(set(scan))
    assert not gone, f"inventory rows the scan no longer finds (delete or rename them): {gone}"


def test_each_key_is_listed_once_with_known_shapes():
    rows = inventory.rows()
    assert len(rows) == len(inventory.OWNERS) + len(inventory.GAPS) + len(inventory.EXCLUSIONS)
    for key, row in rows.items():
        module, _, symbol = key.partition(":")
        assert module.startswith("src") and symbol, key
        assert row.shapes and row.shapes <= inventory.SHAPES, key


def test_owners_name_a_real_domain_and_its_a33_1_bundle():
    assert inventory.OWNERS
    for row in inventory.OWNERS:
        assert row.domain in DOMAINS, row.key
        assert row.bundle == DOMAIN_BUNDLES[row.domain] and row.bundle in BUNDLE_NAMES, row.key
        assert row.provider in inventory.PROVIDERS, row.key
        # Required exactly for the pinned seraph-trust services (ADR-036 §3).
        assert row.required is (row.bundle == TRUST_BUNDLE), row.key


def test_gaps_are_unowned_and_name_their_bundle_owner_and_reason():
    assert inventory.GAPS
    for row in inventory.GAPS:
        assert row.target not in DOMAINS, f"{row.key} has an owner domain now; move it to OWNERS"
        assert row.target in inventory.PLANNED_SEAMS, row.key
        assert row.bundle == inventory.PLANNED_SEAMS[row.target], row.key
        assert row.bundle is None or row.bundle in BUNDLE_NAMES, row.key
        assert row.owner in inventory.GAP_OWNERS, row.key
        assert row.provider in inventory.PROVIDERS, row.key
        assert row.reason.strip() and "\n" not in row.reason, row.key
    for target, bundle in inventory.PLANNED_SEAMS.items():
        assert target is None or target not in DOMAINS, target
        assert bundle is None or bundle in BUNDLE_NAMES, target


def test_every_exclusion_has_a_kind_and_a_one_line_reason():
    assert inventory.EXCLUSIONS
    for row in inventory.EXCLUSIONS:
        assert row.kind in inventory.EXCLUSION_KINDS, row.key
        assert row.reason.strip() and "\n" not in row.reason, row.key


def test_system_jobs_map_to_the_domain_that_gates_them(scan):
    jobs = {row.key: row for row in (*inventory.OWNERS, *inventory.GAPS) if "[job=" in row.key}
    found = {key for key, _shape in scan if "[job=" in key}
    assert found and set(jobs) == found
    for key, row in jobs.items():
        gate = job_domain(key.split("[job=", 1)[1].rstrip("]"))
        if gate is not None:
            assert isinstance(row, inventory.Owner) and row.domain == gate, key


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "src"
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return root


PLANTED = """
    import asyncio, subprocess, threading
    from fastapi import APIRouter, FastAPI

    router = APIRouter()

    class Planted:
        async def start(self):
            asyncio.create_task(self.run())
        async def stop(self):
            pass

    @router.websocket("/ws/planted")
    async def planted_socket(websocket):
        pass

    def schedule(scheduler):
        scheduler.add_job(print, id="planted")
        return [{"func": print, "trigger": None, "id": "planted_job"}]

    def spawn():
        subprocess.Popen(["true"])
        threading.Thread(target=spawn)

    def make():
        return FastAPI()

    def not_spawning(repository):
        return repository.create_task(title="a board row, not an asyncio task")
"""


def test_scan_finds_each_planted_shape(tmp_path):
    found = inventory.scan_lifecycle_shapes(_tree(tmp_path, {"pkg/planted.py": PLANTED}))
    assert found == {
        ("src.pkg.planted:router", "router"),
        ("src.pkg.planted:Planted", "lifecycle_method"),
        ("src.pkg.planted:Planted.start", "background_task"),
        ("src.pkg.planted:planted_socket", "websocket"),
        ("src.pkg.planted:schedule", "scheduler_job"),
        ("src.pkg.planted:schedule[job=planted_job]", "scheduler_job"),
        ("src.pkg.planted:spawn", "subprocess"),
        ("src.pkg.planted:spawn", "thread"),
        ("src.pkg.planted:make", "app_factory"),
    }


def test_guard_fails_on_a_planted_unmapped_owner(tmp_path, scan):
    planted = inventory.scan_lifecycle_shapes(_tree(tmp_path, {"pkg/planted.py": PLANTED}))
    assert inventory.unaccounted(set(scan) | planted) == sorted(planted)
    # A new shape inside an already-listed owner is also caught.
    extra = ("src.conversation.task_context:TaskContinuityService", "subprocess")
    assert inventory.unaccounted(set(scan) | {extra}) == [extra]


def test_scan_keys_lifespan_steps_by_callee(tmp_path):
    source = """
        async def _lifespan_services(app):
            import logging
            logging.getLogger(__name__).warning("noise")
            await planted_service.start()
            async with planted_scope():
                pass
    """
    found = inventory.scan_lifecycle_shapes(_tree(tmp_path, {"app.py": source}))
    assert found == {("src.app:_lifespan_services[step=planted_service.start]", "lifespan_step"),
                     ("src.app:_lifespan_services[step=planted_scope]", "lifespan_step")}


def test_duplicate_keys_are_refused(monkeypatch):
    monkeypatch.setattr(inventory, "EXCLUSIONS", (*inventory.EXCLUSIONS, inventory.EXCLUSIONS[0]))
    with pytest.raises(ValueError, match="listed twice"):
        inventory.rows()
