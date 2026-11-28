"""#1206 (A3.3-3, ADR-036 §6): every composition environment flag is a catalogued
patch row or a classified security switch, env-only installs keep their
behaviour, and the env base layer has an audited removal path that starts with
``SCHEDULER_ENABLED``. Real files, the real settings model and the real readers;
no network."""
from __future__ import annotations

import dataclasses
import inspect
import json
from unittest.mock import patch

import pytest
from sqlmodel import select

from config.settings import Settings
from src.runtime_plugins import inventory, profiles
from src.runtime_plugins.bridge import CordisHost
from src.runtime_plugins.composition_dump import live_composition_dump, offline_composition_dump
from src.runtime_plugins.profiles import (DELEGATION_ROW, END_OF_DAY_REPORT_ROW, ENV_DEPRECATED_EVENT,
                                          ENV_IGNORED_EVENT, PATCH_ROWS, SCHEDULER_ROW,
                                          SCREENSHOT_FOLDER_INGEST_ROW, SCREENSHOT_OBSERVATION_DIGEST_ROW,
                                          SECURITY_SETTINGS, CompositionProfileInvalid, compose_profile,
                                          env_base_layer, env_base_notices, explicit_env_values)

REMOVAL_ISSUES = {SCHEDULER_ROW: "#1337", DELEGATION_ROW: "#1338", SCREENSHOT_FOLDER_INGEST_ROW: "#1339",
                  SCREENSHOT_OBSERVATION_DIGEST_ROW: "#1340", END_OF_DAY_REPORT_ROW: "#1341"}


def _compose(*, patch_rows=None, env_values=None, environment="development"):
    return compose_profile(None, deployment_environment=environment, local_no_login=False,
                           operator_patch=patch_rows, env_values=env_values)


def _refused(reason, **kwargs):
    with pytest.raises(CompositionProfileInvalid) as refused:
        _compose(**kwargs)
    assert refused.value.reason == reason, refused.value.reason
    return refused.value


# --- The inventory: every flag is classified ------------------------------------------------


def test_every_flag_the_scan_finds_is_classified_and_no_entry_is_stale():
    found = inventory.scan_env_flags()
    listed = {flag.env_name: flag for flag in inventory.ENV_FLAGS}
    unclassified = sorted(set(found) - set(listed))
    assert not unclassified, (
        "composition env flags with no entry in runtime_plugins/inventory.py ENV_FLAGS; give each a patch row "
        f"(kind row) or classify it as a security switch: {unclassified}")
    env_reads, _dynamic = inventory.scan_env_reads()
    stale = sorted(set(listed) - set(found) - set(env_reads) - inventory.STRING_SELECTORS)
    assert not stale, f"ENV_FLAGS entries the scan no longer finds: {stale}"
    # A flag read only from the process environment is not a settings field.
    for name in set(listed) - set(found) - inventory.STRING_SELECTORS:
        assert listed[name].setting is None, name
    for name, setting in found.items():
        assert listed[name].setting == setting, name
    # String selectors are named by review; each is a real settings field.
    for name in inventory.STRING_SELECTORS:
        assert name.lower() in Settings.model_fields, name
    assert not inventory.env_flag_problems(), inventory.env_flag_problems()


def test_every_composition_flag_is_a_row_or_a_security_switch():
    kinds = {flag.env_name: flag.kind for flag in inventory.ENV_FLAGS}
    assert {name for name, kind in kinds.items() if kind == "row"} == {
        "SCHEDULER_ENABLED", "USE_DELEGATION", "SCREENSHOT_FOLDER_INGEST_ENABLED",
        "SCREENSHOT_OBSERVATION_DIGEST_ENABLED", "END_OF_DAY_REPORT_ENABLED"}
    # Only diagnostics are outside both classes; nothing is removed yet.
    assert {name for name, kind in kinds.items() if kind == "not_composition"} == {
        "DEBUG", "DATABASE_ECHO", "TRACE_WAS_ENABLED"}
    assert "removed" not in kinds.values()
    # The operator's launcher flags stay environment settings.
    assert kinds["DAEMON_ENABLED"] == kinds["PROXY_ENABLED"] == "security_switch"
    assert {name.lower() for name, kind in kinds.items() if kind == "security_switch"} <= SECURITY_SETTINGS


def test_every_env_var_src_reads_is_classified():
    found, dynamic = inventory.scan_env_reads()
    flags = {flag.env_name for flag in inventory.ENV_FLAGS}
    unclassified = sorted(set(found) - flags - set(inventory.ENV_READS))
    assert not unclassified, (
        "environment variables backend/src reads with no classification; add a flag to ENV_FLAGS or a "
        f"non-flag read to ENV_READS in runtime_plugins/inventory.py: {unclassified}")
    assert not set(inventory.ENV_READS) & flags
    assert not sorted(set(inventory.ENV_READS) - set(found)), "stale ENV_READS entries"
    assert {kind for kind, _reason in inventory.ENV_READS.values()} <= inventory.ENV_READ_KINDS
    assert dynamic == set(inventory.DYNAMIC_ENV_READS), (
        "env reads whose name is computed at runtime must be listed in DYNAMIC_ENV_READS: "
        f"new {sorted(dynamic - set(inventory.DYNAMIC_ENV_READS))}, "
        f"stale {sorted(set(inventory.DYNAMIC_ENV_READS) - dynamic)}")
    # The observer's alias and its helper-read defaults are classified security switches.
    kinds = {flag.env_name: flag.kind for flag in inventory.ENV_FLAGS}
    for name in ("SERAPH_SCREEN_ANALYSIS_PROVIDER", "SERAPH_SCREEN_ANALYSIS_ENABLED",
                 "SERAPH_PRESERVE_SCREEN_CAPTURES", "DAEMON_ENABLED"):
        assert name in found and kinds[name] == "security_switch", name
    # Positional reader helpers: the production workspace resolver's _env_value(values, NAME).
    assert inventory.ENV_READS["BACKEND_DATA_PATH_PROD"][0] == "path"
    assert inventory.ENV_READS["SERAPH_PRODUCTION_MOUNT_SOURCE"][0] == "server_process"
    assert "src.workspace.production:validate_container_workspace_mount" in found["SERAPH_PRODUCTION_MOUNT_SOURCE"]


def test_a_planted_env_read_fails_the_guard(tmp_path):
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "names.py").write_text('PLANTED_NAME = "SERAPH_PLANTED_SWITCH"\n')
    (root / "pkg" / "reader.py").write_text(
        "import os\nfrom os import environ\nfrom src.pkg.names import PLANTED_NAME as ALIAS\n"
        "def read(name):\n"
        "    a = os.environ.get(ALIAS)\n    b = os.getenv('SERAPH_PLANTED_GETENV')\n"
        "    c = environ['SERAPH_PLANTED_SUBSCRIPT']\n    d = 'SERAPH_PLANTED_IN' in os.environ\n"
        "    e = _env_enabled('SERAPH_PLANTED_HELPER')\n    f = _env_value(os.environ, 'SERAPH_PLANTED_POSITIONAL')\n"
        "    return os.environ.get(name)\n")
    (root / "pkg" / "aliases.py").write_text(
        "import os as o\nfrom os import environ as E, getenv as G\nENV = o.environ\n"
        "def read():\n    return (o.getenv('SERAPH_PLANTED_OS_ALIAS'), E['SERAPH_PLANTED_ENVIRON_ALIAS'],\n"
        "            G('SERAPH_PLANTED_GETENV_ALIAS'), ENV.get('SERAPH_PLANTED_ASSIGNED_ALIAS'))\n")
    found, dynamic = inventory.scan_env_reads(root)
    assert set(found) == {"SERAPH_PLANTED_SWITCH", "SERAPH_PLANTED_GETENV", "SERAPH_PLANTED_SUBSCRIPT",
                          "SERAPH_PLANTED_IN", "SERAPH_PLANTED_HELPER", "SERAPH_PLANTED_POSITIONAL",
                          "SERAPH_PLANTED_OS_ALIAS", "SERAPH_PLANTED_ENVIRON_ALIAS", "SERAPH_PLANTED_GETENV_ALIAS",
                          "SERAPH_PLANTED_ASSIGNED_ALIAS"}
    assert dynamic == {"src.pkg.reader:read"}


@pytest.mark.parametrize("annotation", [
    "bool", "StrictBool", "bool | None", "None | bool", "Optional[bool]", "typing.Optional[bool]",
    "Union[bool, None]", "Annotated[bool, Field(description='x')]", "Annotated[Optional[bool], 1]", "'bool'"])
def test_the_settings_scan_unwraps_boolean_annotations(tmp_path, annotation):
    settings_file = tmp_path / "settings.py"
    settings_file.write_text(inventory.SETTINGS_FILE.read_text().replace(
        "    scheduler_enabled: bool = True\n", f"    scheduler_enabled: bool = True\n    planted_wrapped: {annotation} = None\n"))
    assert "PLANTED_WRAPPED" in inventory.scan_env_flags(settings_file)
    settings_file.write_text(inventory.SETTINGS_FILE.read_text().replace(
        "    scheduler_enabled: bool = True\n", "    scheduler_enabled: bool = True\n    planted_text: str | None = None\n"))
    assert "PLANTED_TEXT" not in inventory.scan_env_flags(settings_file)


def test_a_planted_unclassified_flag_fails_the_guard(tmp_path):
    settings_file = tmp_path / "settings.py"
    settings_file.write_text(inventory.SETTINGS_FILE.read_text().replace(
        "    scheduler_enabled: bool = True\n",
        "    scheduler_enabled: bool = True\n    planted_composition_switch: bool = False\n"))
    launcher = tmp_path / "manage.sh"
    launcher.write_text(inventory.LAUNCHER_FILE.read_text() + '\nif [ "${PLANTED_ENABLED:-false}" = true ]; then :; fi\n'
                        'if [ "$UNBRACED_ENABLED" = true ]; then :; fi\n')
    found = inventory.scan_env_flags(settings_file, launcher)
    listed = {flag.env_name for flag in inventory.ENV_FLAGS}
    assert set(found) - listed == {"PLANTED_COMPOSITION_SWITCH", "PLANTED_ENABLED", "UNBRACED_ENABLED"}
    assert found["PLANTED_ENABLED"] is None and found["UNBRACED_ENABLED"] is None


@pytest.mark.parametrize(("flags", "problem"), [
    ((inventory.EnvFlag("USE_DELEGATION", "use_delegation", "security_switch", None, (), "x"),),
     "USE_DELEGATION: not_in_security_settings"),
    ((inventory.EnvFlag("SCHEDULER_ENABLED", "scheduler_enabled", "row", "seraph.scheduler.v1#nonesuch", (), "x"),),
     "SCHEDULER_ENABLED: row_missing"),
    ((inventory.EnvFlag("USE_DELEGATION", "use_delegation", "row", SCHEDULER_ROW, (), "x"),),
     "USE_DELEGATION: row_env_mismatch"),
    ((inventory.EnvFlag("SCHEDULER_ENABLED", "scheduler_enabled", "removed", SCHEDULER_ROW, (), "x"),),
     "SCHEDULER_ENABLED: removed_stage_mismatch"),
    ((inventory.EnvFlag("DEBUG", "debug", "not_composition", None, ("src.nowhere:gate",), "x"),),
     "DEBUG: gate src.nowhere:gate is not an inventory row"),
    ((), f"{DELEGATION_ROW}: patch row has no flag entry"),
])
def test_env_flag_problems_catch_contradictions(flags, problem):
    assert problem in inventory.env_flag_problems(flags)


# --- Rows: typed schemas ---------------------------------------------------------------------


def test_each_row_has_a_typed_schema_a_true_default_and_a_removal_issue():
    assert {row_id: row.removal_issue for row_id, row in PATCH_ROWS.items()} == REMOVAL_ISSUES
    for row in PATCH_ROWS.values():
        field = Settings.model_fields[row.env_setting]
        assert field.annotation is bool and row.default is field.default, row.row_id
        assert row.env_name == row.env_setting.upper()
        assert set(row.values) <= {False, True} and False in row.values
    # Screen observation and screen-derived prompts are disable-only.
    assert PATCH_ROWS[SCREENSHOT_FOLDER_INGEST_ROW].values == (False,)
    assert PATCH_ROWS[SCREENSHOT_OBSERVATION_DIGEST_ROW].values == (False,)
    # The end-of-day report is disable-only too: a patch never undoes END_OF_DAY_REPORT_ENABLED=false.
    assert PATCH_ROWS[END_OF_DAY_REPORT_ROW].values == (False,)
    assert PATCH_ROWS[DELEGATION_ROW].values == (False, True)
    # SCHEDULER_ENABLED is first on the removal path.
    assert {row_id: row.env_stage for row_id, row in PATCH_ROWS.items()} == {
        SCHEDULER_ROW: "deprecated", **{row_id: "supported" for row_id in REMOVAL_ISSUES if row_id != SCHEDULER_ROW}}


def test_a_patch_can_disable_but_never_enable_a_consent_gated_row():
    for row_id in (SCREENSHOT_FOLDER_INGEST_ROW, SCREENSHOT_OBSERVATION_DIGEST_ROW, END_OF_DAY_REPORT_ROW):
        assert _compose(patch_rows=[{"id": row_id, "value": False}]).value(row_id) == (True, False)
        refused = _refused("patch_value_consent_gated", patch_rows=[{"id": row_id, "value": True}])
        assert refused.row_id == row_id
    both = _compose(patch_rows=[{"id": DELEGATION_ROW, "value": True}, {"id": END_OF_DAY_REPORT_ROW, "value": False}])
    assert both.value(DELEGATION_ROW) == (True, True) and both.value(END_OF_DAY_REPORT_ROW) == (True, False)
    _refused("patch_value_invalid", patch_rows=[{"id": DELEGATION_ROW, "value": "true"}])


@pytest.mark.parametrize("flag", [flag for flag in inventory.ENV_FLAGS if flag.kind == "security_switch"],
                         ids=lambda flag: flag.env_name)
def test_no_security_switch_can_be_a_patch_row(flag):
    for row_id in (f"seraph.scheduler.v1#{flag.env_name.lower()}", flag.env_name, flag.env_name.lower()):
        _refused("patch_row_security_setting", patch_rows=[{"id": row_id, "value": True}])


# --- The env base layer: env-only installs keep their behaviour ----------------------------


def test_explicit_env_values_reads_only_what_the_environment_sets(monkeypatch):
    monkeypatch.setenv("SERAPH_SETTINGS_ENV_FILE", "")
    for row in PATCH_ROWS.values():
        monkeypatch.delenv(row.env_name, raising=False)
    monkeypatch.setenv("USE_DELEGATION", "true")
    monkeypatch.setenv("SCREENSHOT_FOLDER_INGEST_ENABLED", "false")
    loaded = Settings()
    assert explicit_env_values(loaded) == {"use_delegation": True, "screenshot_folder_ingest_enabled": False}
    composed = _compose(env_values=explicit_env_values(loaded))
    assert {item.row_id: item.source for item in composed.env_base} == {
        SCHEDULER_ROW: "default", DELEGATION_ROW: "env", SCREENSHOT_FOLDER_INGEST_ROW: "env",
        SCREENSHOT_OBSERVATION_DIGEST_ROW: "default", END_OF_DAY_REPORT_ROW: "default"}


def test_the_env_file_sets_a_row_explicitly(tmp_path, monkeypatch):
    for row in PATCH_ROWS.values():
        monkeypatch.delenv(row.env_name, raising=False)
    env_file = tmp_path / ".env.dev"
    env_file.write_text("SCHEDULER_ENABLED=false\nEND_OF_DAY_REPORT_ENABLED=true\n")
    monkeypatch.setenv("SERAPH_SETTINGS_ENV_FILE", str(env_file))
    from config.settings import resolve_settings_env_file
    loaded = Settings(_env_file=resolve_settings_env_file())
    assert explicit_env_values(loaded) == {"scheduler_enabled": False, "end_of_day_report_enabled": True}


@pytest.mark.parametrize("row_id", list(REMOVAL_ISSUES))
def test_without_a_patch_every_reader_returns_the_environment_value(row_id, monkeypatch):
    helper = {SCHEDULER_ROW: profiles.scheduler_enabled, DELEGATION_ROW: profiles.delegation_enabled,
              SCREENSHOT_FOLDER_INGEST_ROW: profiles.screenshot_folder_ingest_enabled,
              SCREENSHOT_OBSERVATION_DIGEST_ROW: profiles.screenshot_observation_digest_enabled,
              END_OF_DAY_REPORT_ROW: profiles.end_of_day_report_enabled}[row_id]
    spec = PATCH_ROWS[row_id]
    for value in (False, True):
        monkeypatch.setattr(profiles, "_booted", _compose(env_values={spec.env_setting: value}))
        assert helper(value) is value
        # The live settings attribute is the base when the caller passes none.
        from config.settings import settings
        monkeypatch.setattr(settings, spec.env_setting, value)
        assert helper() is value
    # The last patch layer wins over the environment base layer.
    monkeypatch.setattr(profiles, "_booted", _compose(patch_rows=[{"id": row_id, "value": False}]))
    assert helper(True) is False


# --- The removal path ------------------------------------------------------------------------


def test_a_deprecated_flag_is_still_honoured_and_noticed():
    composed = _compose(env_values={"scheduler_enabled": False})
    assert profiles.booted_profile() is None
    notices = env_base_notices(composed)
    assert notices == [{"event_type": ENV_DEPRECATED_EVENT, "id": SCHEDULER_ROW, "env": "SCHEDULER_ENABLED",
                        "env_stage": "deprecated", "env_set": True, "source": "env", "removal_issue": "#1337"}]
    # Supported flags that are set raise no notice; an unset deprecated flag raises none.
    assert env_base_notices(_compose(env_values={"use_delegation": True})) == []
    # A patch row that covers the deprecated flag: the patch wins, and the stale env value is noticed.
    # The row's stage, not the source, chooses the event: still deprecated, overridden by the patch.
    covered = _compose(env_values={"scheduler_enabled": False}, patch_rows=[{"id": SCHEDULER_ROW, "value": True}])
    assert covered.value(SCHEDULER_ROW) == (True, True)
    assert [(notice["event_type"], notice["source"], notice["env_stage"]) for notice in env_base_notices(covered)] == [
        (ENV_DEPRECATED_EVENT, "patch", "deprecated")]


def _retired(row_id):
    return {**PATCH_ROWS, row_id: dataclasses.replace(PATCH_ROWS[row_id], env_stage="retired")}


def test_a_retired_flag_never_changes_behaviour_silently(monkeypatch):
    rows = _retired(SCHEDULER_ROW)
    # Unset or set to the default: ignored.
    assert {item.row_id: item.source for item in env_base_layer([], {}, rows)}[SCHEDULER_ROW] == "default"
    assert {item.row_id: item.source for item in env_base_layer([], {"scheduler_enabled": True}, rows)}[
        SCHEDULER_ROW] == "ignored"
    # A non-default value with no patch row refuses boot with recovery text naming the row.
    with pytest.raises(CompositionProfileInvalid) as refused:
        env_base_layer([], {"scheduler_enabled": False}, rows)
    assert (refused.value.reason, refused.value.row_id) == ("env_flag_retired", SCHEDULER_ROW)
    monkeypatch.setitem(PATCH_ROWS, SCHEDULER_ROW, rows[SCHEDULER_ROW])
    text = profiles.recovery_text(refused.value)
    assert "SCHEDULER_ENABLED" in text and SCHEDULER_ROW in text and "#1337" in text
    _refused("env_flag_retired", env_values={"scheduler_enabled": False})
    # The same value in a patch row: the patch applies and the env value is ignored.
    covered = _compose(env_values={"scheduler_enabled": False}, patch_rows=[{"id": SCHEDULER_ROW, "value": False}])
    assert {item.row_id: item.source for item in covered.env_base}[SCHEDULER_ROW] == "patch"
    assert [notice["event_type"] for notice in env_base_notices(covered)] == [ENV_IGNORED_EVENT]
    # Readers never consult the retired env flag again.
    monkeypatch.setattr(profiles, "_booted", _compose(env_values={"scheduler_enabled": True}))
    assert profiles.scheduler_enabled(False) is True


def test_an_unknown_env_stage_is_refused():
    rows = {SCHEDULER_ROW: dataclasses.replace(PATCH_ROWS[SCHEDULER_ROW], env_stage="someday")}
    with pytest.raises(CompositionProfileInvalid) as refused:
        env_base_layer([], {}, rows)
    assert refused.value.reason == "env_stage_unknown"


@pytest.mark.asyncio
async def test_boot_audits_a_deprecated_flag_the_environment_still_sets(db, monkeypatch):
    from src.db.engine import get_session
    from src.db.models import AuditEvent
    monkeypatch.setattr(profiles, "_booted", _compose(env_values={"scheduler_enabled": False,
                                                                    "use_delegation": False}))
    notices = await profiles.record_env_base_layer()
    assert [notice["env"] for notice in notices] == ["SCHEDULER_ENABLED"]
    async with get_session() as session:
        events = list((await session.execute(
            select(AuditEvent).where(AuditEvent.event_type == ENV_DEPRECATED_EVENT))).scalars())
    assert len(events) == 1
    assert json.loads(events[0].details_json) == {"id": SCHEDULER_ROW, "env": "SCHEDULER_ENABLED",
                                                  "env_stage": "deprecated", "source": "env",
                                                  "removal_issue": "#1337"}
    assert "composition.patch.json" in events[0].summary and "#1337" in events[0].summary
    # The lifespan runs this step once the provider worker opened the state gate.
    from src import app as app_module
    source = inspect.getsource(app_module._lifespan_services)
    assert source.index("await record_env_base_layer()") < source.index("TaskContinuityService(")


@pytest.mark.asyncio
async def test_a_deprecated_flag_a_patch_overrides_is_audited_as_deprecated(db, monkeypatch, caplog):
    from src.db.engine import get_session
    from src.db.models import AuditEvent
    monkeypatch.setattr(profiles, "_booted", _compose(env_values={"scheduler_enabled": False},
                                                      patch_rows=[{"id": SCHEDULER_ROW, "value": True}]))
    with caplog.at_level("WARNING", logger=profiles.__name__):
        notices = await profiles.record_env_base_layer()
    assert [(notice["event_type"], notice["source"]) for notice in notices] == [(ENV_DEPRECATED_EVENT, "patch")]
    async with get_session() as session:
        events = list((await session.execute(select(AuditEvent).where(
            AuditEvent.event_type.in_([ENV_DEPRECATED_EVENT, ENV_IGNORED_EVENT])))).scalars())
    assert [event.event_type for event in events] == [ENV_DEPRECATED_EVENT]
    summary = events[0].summary
    assert f"deprecated; overridden by patch row {SCHEDULER_ROW}" in summary
    assert "retired" not in summary and "ignored" not in summary
    assert json.loads(events[0].details_json)["source"] == "patch"
    assert "retired" not in caplog.text and "overridden by patch row" in caplog.text


@pytest.mark.asyncio
async def test_an_unwritable_audit_never_blocks_boot(monkeypatch):
    from src.audit.repository import audit_repository

    async def refuse(**_kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(audit_repository, "log_event", refuse)
    monkeypatch.setattr(profiles, "_booted", _compose(env_values={"scheduler_enabled": True}))
    assert [notice["env"] for notice in await profiles.record_env_base_layer()] == ["SCHEDULER_ENABLED"]


# --- Rows change actual activation -------------------------------------------------------------


def test_the_delegation_row_changes_the_delegate_tool(monkeypatch):
    from config.settings import settings
    from src.tools.delegate_task_tool import delegate_task
    monkeypatch.setattr(settings, "use_delegation", True)
    monkeypatch.setattr(profiles, "_booted", _compose(patch_rows=[{"id": DELEGATION_ROW, "value": False}]))
    assert delegate_task(task="summarise") == "Error: Delegation runtime is disabled."


@pytest.mark.asyncio
async def test_the_delegation_row_changes_the_tools_catalogue(client, monkeypatch):
    from config.settings import settings
    from src.observer.context import CurrentContext
    monkeypatch.setattr(settings, "use_delegation", False)
    ctx = CurrentContext(tool_policy_mode="full")
    with patch("src.tools.policy.context_manager.get_context", return_value=ctx):
        assert "delegate_task" not in {tool["name"] for tool in (await client.get("/api/tools")).json()}
        monkeypatch.setattr(profiles, "_booted", _compose(patch_rows=[{"id": DELEGATION_ROW, "value": True}]))
        assert "delegate_task" in {tool["name"] for tool in (await client.get("/api/tools")).json()}


@pytest.mark.asyncio
async def test_the_screenshot_rows_change_the_actual_jobs(db, monkeypatch):
    from config.settings import settings
    from src.scheduler.jobs import screenshot_folder_ingest
    from src.scheduler.screen_llm_policy import screen_derived_llm_decision
    recorded = []

    async def record(**kwargs):
        recorded.append((kwargs["job_name"], kwargs["outcome"], kwargs["details"].get("reason")))

    monkeypatch.setattr(screenshot_folder_ingest, "log_scheduler_job_event", record)
    monkeypatch.setattr(settings, "screenshot_folder_ingest_enabled", True)
    monkeypatch.setattr(settings, "screenshot_observation_digest_enabled", True)
    monkeypatch.setattr(profiles, "_booted", _compose(patch_rows=[
        {"id": SCREENSHOT_FOLDER_INGEST_ROW, "value": False},
        {"id": SCREENSHOT_OBSERVATION_DIGEST_ROW, "value": False}]))
    await screenshot_folder_ingest.run_screenshot_folder_ingest()
    assert recorded == [("screenshot_folder_ingest", "skipped", "disabled")]
    decision = await screen_derived_llm_decision("screenshot_observation_digest")
    assert (decision.allowed, decision.reason) == (False, "llm_disabled")


@pytest.mark.asyncio
async def test_the_settings_api_reports_the_effective_rows(client, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "screenshot_folder_ingest_enabled", True)
    monkeypatch.setattr(settings, "end_of_day_report_enabled", True)
    monkeypatch.setattr(profiles, "_booted", _compose(patch_rows=[
        {"id": SCREENSHOT_FOLDER_INGEST_ROW, "value": False}, {"id": END_OF_DAY_REPORT_ROW, "value": False}]))
    response = await client.get("/api/settings/artifact-storage")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["screenshot_folder"]["auto_ingest_enabled"] is False
    assert data["screenshot_folder"]["control_row"] == {"auto_ingest_enabled": SCREENSHOT_FOLDER_INGEST_ROW}
    assert data["reports"]["enabled"] is False
    assert data["reports"]["control_row"] == {"enabled": END_OF_DAY_REPORT_ROW}


# --- The dump --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_dump_shows_each_rows_env_stage_and_source(db, tmp_path, monkeypatch):
    composed = _compose(env_values={"scheduler_enabled": False},
                        patch_rows=[{"id": DELEGATION_ROW, "value": True}])
    monkeypatch.setattr(profiles, "_booted", composed)
    live = await live_composition_dump(CordisHost())
    assert [row["id"] for row in live["patch_rows"]] == list(PATCH_ROWS)
    assert live["patch_rows"][0] == {"id": SCHEDULER_ROW, "env": "SCHEDULER_ENABLED", "values": [False, True],
                                     "default": True, "env_stage": "deprecated", "removal_issue": "#1337"}
    rows = {row["id"]: row for row in live["env_base_layer"]["rows"]}
    assert live["env_base_layer"]["read"] is True
    assert (rows[SCHEDULER_ROW]["source"], rows[SCHEDULER_ROW]["env_set"]) == ("env", True)
    assert rows[DELEGATION_ROW]["source"] == "patch" and rows[END_OF_DAY_REPORT_ROW]["source"] == "default"
    # Booleans and fixed names only: no settings value is copied into the dump.
    assert all(set(row) == {"id", "env", "env_stage", "env_set", "source", "removal_issue"} for row in rows.values())
    offline = offline_composition_dump(tmp_path)
    assert offline["env_base_layer"] == {"read": False, "rows": []}
    assert offline["patch_rows"] == live["patch_rows"]
