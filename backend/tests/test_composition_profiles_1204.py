"""#1204 (A3.3-1, ADR-036 §3/§4/§6): the bundle catalogue, the named ``local``,
``prod`` and ``headless`` profiles, the layered patch validator, the boot-time
refusal and the composition dump's real ``profile`` and ``effective_patch``.
Real files, the real lifespan entry and the real managed launcher; no network."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

from fastapi import FastAPI
import pytest

from tests.cordis_research import seed_composition_inventory
from tests.test_inference_accounting import accounting_db  # noqa: F401
from src.runtime_plugins import profiles
from src.runtime_plugins.bridge import CordisHost
from src.runtime_plugins.composition_dump import live_composition_dump, main as dump_main, offline_composition_dump
from src.runtime_plugins.ownership import DOMAINS
from src.runtime_plugins.profiles import (BUNDLES, CORE_BUNDLES, OPERATOR_PATCH_FILE, PATCH_ROWS, PATCH_SCHEMA,
                                          PROFILES, CompositionProfileInvalid, Profile, boot_profile,
                                          compose_profile, load_operator_patch, release_profile,
                                          scheduler_enabled)
from src.runtime_plugins.trust import DOMAIN_BUNDLES, TRUST_BUNDLE

REPO = Path(__file__).resolve().parents[2]
SCHEDULER_ROW = "seraph.scheduler.v1#enabled"


@pytest.fixture(autouse=True)
def _no_booted_profile():
    release_profile()
    yield
    release_profile()


def _compose(requested=None, *, environment="development", no_login=False, patch=None, catalogue=None):
    return compose_profile(requested, deployment_environment=environment, local_no_login=no_login,
                           operator_patch=patch, profiles=catalogue)


def _refused(reason, **kwargs):
    with pytest.raises(CompositionProfileInvalid) as refused:
        _compose(**kwargs)
    assert refused.value.reason == reason, refused.value.reason
    return refused.value


def _write_patch(workspace: Path, rows, *, mode=0o600) -> Path:
    path = workspace / OPERATOR_PATCH_FILE
    path.write_text(json.dumps({"schema": PATCH_SCHEMA, "rows": rows}))
    path.chmod(mode)
    return path


def test_the_bundle_catalogue_is_the_adr_table_and_every_domain_has_one_bundle():
    assert [bundle.name for bundle in BUNDLES] == [
        "seraph-trust", "seraph-agent", "seraph-models", "seraph-capabilities", "seraph-domains",
        "seraph-lifecycle", "seraph-cockpit", "seraph-channels", "seraph-edge", "seraph-headless"]
    assert [bundle.kind for bundle in BUNDLES][:6] == ["trust", *["core"] * 5]
    assert {domain for bundle in BUNDLES for domain in bundle.domains} == set(DOMAINS) == set(DOMAIN_BUNDLES)
    # App bundles carry no domain row yet; their interface domains are later A3.3 slices.
    assert all(not bundle.domains for bundle in BUNDLES if bundle.kind == "app")
    # Every catalogued patch row sits on a non-pinned domain.
    assert all(DOMAIN_BUNDLES[row.runtime_domain] != TRUST_BUNDLE for row in PATCH_ROWS.values())
    assert set(PROFILES) == {"local", "prod", "headless"} and "test" not in PROFILES
    for profile in PROFILES.values():
        assert profile.bundles[0] == TRUST_BUNDLE and set(CORE_BUNDLES) <= set(profile.bundles)
        assert profile.patch == ()
    assert "seraph-cockpit" not in PROFILES["headless"].bundles
    assert "seraph-cockpit" in PROFILES["local"].bundles and "seraph-cockpit" in PROFILES["prod"].bundles


@pytest.mark.parametrize(("environment", "expected"), [
    ("development", "local"), ("test", "local"), ("production", "prod"), (" Prod ", "prod")])
def test_an_existing_install_derives_its_profile_from_the_deployment_environment(environment, expected):
    composed = _compose(None, environment=environment)
    assert (composed.profile.profile_id, composed.source) == (expected, "deployment_environment")
    assert composed.applied == () and composed.effective_patch == []
    assert _compose("", environment=environment).profile.profile_id == expected


def test_headless_is_selectable_in_either_environment():
    for environment in ("development", "production"):
        composed = _compose("headless", environment=environment)
        assert composed.source == "explicit" and composed.profile.env_file is None


def test_profile_selection_refusals():
    _refused("profile_unknown", requested="test")
    _refused("profile_unknown", requested="dsh-base")
    _refused("profile_environment_mismatch", requested="local", environment="production")
    _refused("profile_environment_mismatch", requested="prod", environment="development")
    _refused("profile_environment_mismatch", requested="prod", environment="")
    # Startup validation already refuses no-login outside development; a profile never carries it.
    _refused("profile_no_login_forbidden", environment="production", no_login=True)
    _refused("profile_no_login_forbidden", requested="headless", environment="production", no_login=True)


@pytest.mark.parametrize(("bundles", "reason"), [
    ((), "profile_trust_bundle_missing"),
    (("seraph-agent", "seraph-trust"), "profile_trust_bundle_missing"),
    (("seraph-agent",), "profile_trust_bundle_missing"),
    (("seraph-trust", "seraph-agent"), "profile_core_bundle_missing"),
    (("seraph-trust", *CORE_BUNDLES, "dsh-base"), "profile_bundle_unknown"),
    (("seraph-trust", *CORE_BUNDLES, "seraph-trust"), "profile_bundle_duplicate"),
])
def test_a_profile_that_omits_or_reorders_the_pinned_trust_bundle_is_refused(bundles, reason):
    catalogue = {"local": Profile("local", bundles, frozenset({"development"}), ".env.dev")}
    _refused(reason, requested="local", catalogue=catalogue)


@pytest.mark.parametrize(("row", "reason"), [
    # Pinned services: no layer may touch, replace or reconfigure them (§3).
    ({"id": "seraph.tasks.v1#enabled", "value": False}, "patch_row_pinned"),
    ({"id": "seraph.vault.v1#enabled", "value": False}, "patch_row_pinned"),
    ({"id": "seraph.inference.v1#enabled", "value": True}, "patch_row_pinned"),
    ({"id": "seraph.state.v1#provider", "provider": "dsh-jsonl"}, "patch_row_pinned"),
    ({"id": "seraph.authority.v1#grants", "value": True}, "patch_row_pinned"),
    # Security switches stay startup-validated environment settings (§6).
    ({"id": "operator_auth_local_no_login", "value": True}, "patch_row_security_setting"),
    ({"id": "OPERATOR-AUTH-LOCAL-NO-LOGIN", "value": True}, "patch_row_security_setting"),
    ({"id": "seraph.scheduler.v1#operator_auth_local_no_login", "value": True}, "patch_row_security_setting"),
    ({"id": "seraph.agent-loop.v1#deployment_environment", "value": "development"}, "patch_row_security_setting"),
    # Credentials never travel in a patch: only typed, catalogued values pass.
    ({"id": SCHEDULER_ROW, "value": "ghp_1204_never_a_patch_value"}, "patch_value_invalid"),
    ({"id": SCHEDULER_ROW, "value": 1}, "patch_value_invalid"),
    ({"id": SCHEDULER_ROW, "value": None}, "patch_value_invalid"),
    ({"id": "seraph.connections.v1#token", "value": "secret"}, "patch_row_unknown"),
    ({"id": "seraph.agent-loop.v1#enabled", "value": True}, "patch_row_unknown"),
    ({"id": "seraph.scheduler.v1#provider", "provider": "cron"}, "patch_provider_unreviewed"),
    ({"id": SCHEDULER_ROW, "value": True, "grant": "operator:root"}, "patch_row_invalid"),
    ({"id": SCHEDULER_ROW}, "patch_row_invalid"),
    ({"id": "not a row", "value": True}, "patch_row_invalid"),
    ({"value": True}, "patch_row_invalid"),
    ("seraph.scheduler.v1#enabled=false", "patch_row_invalid"),
])
def test_the_operator_patch_is_a_closed_allow_list(row, reason):
    _refused(reason, patch=[row])


def test_patch_shape_refusals():
    _refused("patch_invalid", patch={"rows": []})
    _refused("patch_row_duplicate", patch=[{"id": SCHEDULER_ROW, "value": True},
                                           {"id": SCHEDULER_ROW, "value": False}])
    _refused("patch_too_large", patch=[{"id": SCHEDULER_ROW, "value": True}] * 65)


def test_a_profile_patch_is_validated_by_the_same_rules():
    pinned = {"local": Profile("local", PROFILES["local"].bundles, frozenset({"development"}), ".env.dev",
                               ({"id": "seraph.tasks.v1#enabled", "value": False},))}
    _refused("patch_row_pinned", requested="local", catalogue=pinned)


def test_layers_apply_in_order_and_the_operator_layer_wins():
    catalogue = {"local": Profile("local", PROFILES["local"].bundles, frozenset({"development"}), ".env.dev",
                                  ({"id": SCHEDULER_ROW, "value": False},))}
    profile_only = _compose("local", catalogue=catalogue)
    assert profile_only.effective_patch == [{"id": SCHEDULER_ROW, "value": False, "layer": "profile"}]
    both = _compose("local", catalogue=catalogue, patch=[{"id": SCHEDULER_ROW, "value": True}])
    assert both.applied == ((SCHEDULER_ROW, False, "profile"), (SCHEDULER_ROW, True, "operator"))
    assert both.effective_patch == [{"id": SCHEDULER_ROW, "value": True, "layer": "operator"}]
    assert both.value(SCHEDULER_ROW) == (True, True)
    assert both.digest != profile_only.digest
    assert _compose("local").digest == _compose("local").digest != _compose("headless").digest


def test_the_operator_patch_file_is_read_safely(tmp_path):
    assert load_operator_patch(tmp_path) is None
    _write_patch(tmp_path, [{"id": SCHEDULER_ROW, "value": False}])
    assert load_operator_patch(tmp_path) == [{"id": SCHEDULER_ROW, "value": False}]
    path = tmp_path / OPERATOR_PATCH_FILE
    for mode in (0o620, 0o602):
        path.chmod(mode)
        with pytest.raises(CompositionProfileInvalid) as unsafe:
            load_operator_patch(tmp_path)
        assert unsafe.value.reason == "operator_patch_unsafe"
    path.chmod(0o600)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path.rename(elsewhere / OPERATOR_PATCH_FILE)
    (tmp_path / OPERATOR_PATCH_FILE).symlink_to(elsewhere / OPERATOR_PATCH_FILE)
    with pytest.raises(CompositionProfileInvalid) as linked:
        load_operator_patch(tmp_path)
    assert linked.value.reason == "operator_patch_unsafe"
    (tmp_path / OPERATOR_PATCH_FILE).unlink()
    for body, reason in ((b"{", "operator_patch_invalid"),
                         (b'{"schema": "seraph-composition-patch.v1", "rows": [], "rows": []}',
                          "operator_patch_invalid"),
                         (b'{"schema": "cordis.patch", "rows": []}', "operator_patch_invalid"),
                         (b'{"schema": "seraph-composition-patch.v1"}', "operator_patch_invalid"),
                         (b" " * (16 * 1024 + 1), "operator_patch_too_large")):
        (tmp_path / OPERATOR_PATCH_FILE).write_bytes(body)
        (tmp_path / OPERATOR_PATCH_FILE).chmod(0o600)
        with pytest.raises(CompositionProfileInvalid) as invalid:
            load_operator_patch(tmp_path)
        assert invalid.value.reason == reason


@pytest.mark.asyncio
async def test_a_patch_row_changes_the_actual_scheduler_activation(tmp_path, monkeypatch):
    from config.settings import settings
    from src.scheduler.engine import init_scheduler, shutdown_scheduler
    from src.scheduler.background_inference import _scheduler_enabled
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_environment", "development")
    monkeypatch.setattr(settings, "composition_profile", "")
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    # No patch: the environment flag is the base layer, read live.
    assert boot_profile().effective_patch == [] and scheduler_enabled() is True
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    assert scheduler_enabled() is False
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    _write_patch(tmp_path, [{"id": SCHEDULER_ROW, "value": False}])
    composed = boot_profile()
    assert composed.effective_patch == [{"id": SCHEDULER_ROW, "value": False, "layer": "operator"}]
    assert scheduler_enabled() is False and _scheduler_enabled() is False
    try:
        assert init_scheduler() is None
    finally:
        shutdown_scheduler()
    release_profile()
    assert scheduler_enabled() is True


@pytest.mark.asyncio
async def test_the_lifespan_refuses_an_invalid_composition_before_any_provider_starts(tmp_path, monkeypatch):
    from config.settings import settings
    from src.app import lifespan
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_environment", "development")
    monkeypatch.setattr(settings, "composition_profile", "")
    _write_patch(tmp_path, [{"id": "seraph.tasks.v1#enabled", "value": False}])
    app = FastAPI()
    with pytest.raises(CompositionProfileInvalid) as refused:
        async with lifespan(app):
            pytest.fail("a refused composition must not boot")
    assert refused.value.reason == "patch_row_pinned"
    # No provider worker was created, so no lock was taken and no database opened.
    assert not hasattr(app.state, "provider_worker")
    assert not (tmp_path / "seraph.db").exists()
    assert profiles.booted_profile() is None
    monkeypatch.setattr(settings, "composition_profile", "prod")
    (tmp_path / OPERATOR_PATCH_FILE).unlink()
    with pytest.raises(CompositionProfileInvalid) as mismatch:
        async with lifespan(app):
            pytest.fail("prod must not boot in development")
    assert mismatch.value.reason == "profile_environment_mismatch"
    assert "COMPOSITION_PROFILE" in profiles.recovery_text(mismatch.value)
    assert OPERATOR_PATCH_FILE in profiles.recovery_text(refused.value)


def _bundle_status(dump):
    return {bundle["bundle"]: bundle["status"] for bundle in dump["bundles"]}


@pytest.mark.asyncio
async def test_the_dump_shows_the_booted_profile_and_effective_patch(accounting_db, monkeypatch):  # noqa: F811
    from config.settings import settings
    await seed_composition_inventory()
    workspace = Path(settings.workspace_dir)
    monkeypatch.setattr(settings, "composition_profile", "headless")
    monkeypatch.setattr(settings, "deployment_environment", "development")
    _write_patch(workspace, [{"id": SCHEDULER_ROW, "value": False}])
    composed = boot_profile()
    live = await live_composition_dump(CordisHost())
    offline = offline_composition_dump(workspace, profile="headless")
    for dump in (live, offline):
        assert dump["profile"] == {"state": "valid", "reason": None, "profile_id": "headless", "source": "explicit",
                                   "env_file": None, "bundles": list(PROFILES["headless"].bundles),
                                   "operator_patch": "present", "digest": composed.digest}
        assert dump["effective_patch"] == [{"id": SCHEDULER_ROW, "value": False, "layer": "operator"}]
        status = _bundle_status(dump)
        assert status["seraph-cockpit"] == "not_selected" and status["seraph-edge"] == "not_selected"
        assert status["seraph-trust"] == status["seraph-headless"] == "selected"
        assert {bundle["bundle"]: bundle["kind"] for bundle in dump["bundles"]}["seraph-cockpit"] == "app"
    # Before any boot, the live dump says so rather than guessing.
    release_profile()
    assert (await live_composition_dump(CordisHost()))["profile"] is None
    assert set(_bundle_status(await live_composition_dump(CordisHost())).values()) == {"declared"}


@pytest.mark.asyncio
async def test_an_invalid_patch_shows_in_the_offline_dump_without_echoing_its_text(
        accounting_db, capsys):  # noqa: F811
    from config.settings import settings
    await seed_composition_inventory()
    workspace = Path(settings.workspace_dir)
    secret = "ghp_1204_operator_typed_this_into_a_row"
    _write_patch(workspace, [{"id": f"seraph.connections.v1#{secret[:20].lower()}", "value": secret}])
    assert dump_main(["dump", "--workspace", str(workspace)]) == 1
    printed = capsys.readouterr().out
    dump = json.loads(printed)
    assert dump["profile"] == {"state": "invalid", "reason": "patch_row_unknown", "row_id": "redacted"}
    assert dump["effective_patch"] == [] and secret not in printed and secret[:20].lower() not in printed
    _write_patch(workspace, [{"id": SCHEDULER_ROW, "value": True}])
    assert dump_main(["dump", "--workspace", str(workspace)]) == 0
    capsys.readouterr()
    assert dump_main(["dump", "--workspace", str(workspace), "--profile", "prod"]) == 1
    assert json.loads(capsys.readouterr().out)["profile"]["reason"] == "profile_environment_mismatch"


@pytest.mark.asyncio
async def test_manage_sh_composition_dump_passes_the_profile_selection(accounting_db, tmp_path):  # noqa: F811
    from config.settings import settings
    await seed_composition_inventory()
    workspace = Path(settings.workspace_dir)
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    shutil.copy2(REPO / "manage.sh", launcher / "manage.sh")
    (launcher / "backend").symlink_to(REPO / "backend")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    for selection, expected in (("", "local"), ("headless", "headless")):
        (launcher / ".env.dev").write_text(f"LOCAL_WORKSPACE_DIR={workspace}\nCOMPOSITION_PROFILE={selection}\n"
                                           "DEPLOYMENT_ENVIRONMENT=development\n")
        result = subprocess.run(["bash", str(launcher / "manage.sh"), "-e", "dev", "composition", "dump"],
                                cwd=launcher, env=env, capture_output=True, text=True, timeout=120, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["profile"]["profile_id"] == expected
    (launcher / ".env.dev").write_text(f"LOCAL_WORKSPACE_DIR={workspace}\nCOMPOSITION_PROFILE=prod\n")
    refused = subprocess.run(["bash", str(launcher / "manage.sh"), "-e", "dev", "composition", "dump"],
                             cwd=launcher, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["profile"]["reason"] == "profile_environment_mismatch"
