"""ADR-037 (#1284): Seraph keeps no cost limit; spend is history and display only.

Behavioural checks that no dollar amount, month, reserve, overrun or missing
price stops a request, that settings and ledgers written by an older build
still load without losing spend history, and that no removed reason code
survives anywhere in the backend or cockpit sources.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

import pytest

from src.model_fabric.configuration import (
    ModelFabricConfiguration, OpenRouterSetup, write_model_fabric_configuration,
)
from src.model_fabric.remote_inference_admission import (
    RemoteInferenceAdmissionBroker, RemoteInferenceAdmissionRequest, RemoteInferencePriority,
)
from src.security.trust_contract import EgressClass
from src.workflows.job_runtime import DurableJobRepository
from tests.support.accounting import accounting_db  # noqa: F401

# The intercepted legacy OpenRouter broker path is the provider-free harness
# for the shared lane and its request records (see test_inference_accounting).
pytestmark = pytest.mark.openrouter_legacy

REPO_ROOT = Path(__file__).resolve().parents[2]

# Design #1284 section 3.1: none of these may be raised or mapped any more.
REMOVED_REASON_CODES = (
    "accounting_period_review_required", "accounting_clock_correction_required",
    "deployment_cost_budget_exhausted", "owner_cost_budget_exhausted", "owner_budget_exhausted",
    "owner_cost_unknown", "provider_charge_exceeded_reservation",
    "accounting_settings_revision_unavailable", "accounting_server_bound_required",
    "accounting_legacy_reconciliation_required", "inference_lane_required",
    "near_tee_request_reserve_exceeds_lane_ceiling", "near_tee_catalog_cost_bound_too_small",
    "near_tee_cost_overrun", "near_tee_cost_liability", "near_text_cost_overrun",
    "near_cost_overrun_review_required", "near_cost_readback_required", "chat_cost_ceiling_missing",
    "embedding_cost_ceiling_required", "model_fabric_cost_bound_missing", "profile_cost_unknown",
    "profile_cost_noncompliant", "profile_cost_source_missing_or_stale", "request_bound_below_overrun",
    "deployment_spend_ceiling_mismatch", "research_accounting_blocked",
    "research_reviewed_allowance_changed", "review_cost_overrun", "minimum_request_bound_microusd",
    # Part 2: per-task, per-programme and per-job dollar allowances, the
    # charge-gated lane recovery and the ledger latch.
    "general_task_group_cost_limit", "general_task_specialist_cost_limit",
    "general_task_planning_budget_required", "general_task_planning_budget_insufficient",
    "communication_source_budget_unavailable", "communication_source_budget_changed",
    "repository_original_budget_exhausted", "repository_iteration_original_cost_limit",
    "repository_original_unknown_liability", "repository_iteration_unknown",
    "programme_zero_budget", "programme_allowance_exhausted", "programme_generation_cost_budget_exhausted",
    "near_tee_reindex_budget_exhausted", "near_tee_reindex_budget_below_request_reserve",
    "near_tee_reindex_budget_exceeds_lane_ceiling", "reconcile_settled_near_operation", "latched_reason",
)


def _no_limit_configuration():
    """A legacy-mode route that carries no ceiling, no reserve and no lane."""
    setup = OpenRouterSetup(model_ids=("openai/gpt-4o-mini",), capabilities=("text",),
        allowed_upstreams=("openai",), egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True, credential_ref="env:OPENROUTER_API_KEY")
    configuration = ModelFabricConfiguration(status="ready", openrouter_setup=setup)
    write_model_fabric_configuration(configuration)
    return configuration


def _request(operation_id, *, owner="service:accounting"):
    return RemoteInferenceAdmissionRequest(operation_id=operation_id, job_id=operation_id,
        owner_id=owner, priority=RemoteInferencePriority.INTERACTIVE_CHAT, deadline_at=time.time() + 120,
        runtime_path="chat_agent", data_digest="a" * 64)


def _charged(cost):
    async def transport():
        return {"id": "gen-fixture", "usage": {"cost": cost}, "choices": [{"message": {"content": "answer"}}]}
    return transport


async def _unpriced():
    return {"id": "gen-unpriced", "usage": {"prompt_tokens": 3}, "choices": [{"message": {"content": "answer"}}]}


def test_no_removed_reason_code_survives_in_backend_or_cockpit_sources():
    found = []
    for base, suffixes in ((REPO_ROOT / "backend" / "src", {".py"}),
                           (REPO_ROOT / "frontend" / "src", {".ts", ".tsx"})):
        for path in base.rglob("*"):
            if path.suffix not in suffixes or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            found.extend(f"{path.relative_to(REPO_ROOT)}: {code}" for code in REMOVED_REASON_CODES if code in text)
    assert found == []


def test_period_review_endpoint_and_cli_flag_are_gone():
    from src.api.model_fabric_settings import router
    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/settings/model-fabric/accounting/period" not in paths
    assert "/settings/model-fabric/accounting/settle" in paths
    cli = (REPO_ROOT / "backend" / "workspace_cli.py").read_text()
    assert "--period" not in cli and "acknowledge_accounting_period" not in cli


@pytest.mark.asyncio
async def test_request_is_admitted_and_answered_with_no_lane_ceiling_or_bound(accounting_db):
    _no_limit_configuration()
    repository = DurableJobRepository()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    # No settings save created the ledger: the first request creates it.
    result = await broker.execute(_request("no-limit-one"), _charged("0.0000061"))
    assert result["choices"][0]["message"]["content"] == "answer"
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["status"] == "ready"
    assert snapshot["spent_microusd"] == 7
    assert snapshot["unpriced_requests"] == 0
    for removed in ("ceiling_microusd", "remaining_microusd", "reserved_microusd", "unknown_microusd",
                    "committed_microusd", "period_review", "request_reserve_review",
                    "overrun_max_cost_microusd", "unreviewed_overruns", "authorized_period"):
        assert removed not in snapshot
    row = snapshot["operations"][0]
    assert (row["bound_microusd"], row["ceiling_microusd"], row["owner_ceiling_microusd"]) == (0, 0, None)


@pytest.mark.asyncio
async def test_any_charge_is_delivered_settled_and_never_blocks_the_next_request(accounting_db):
    _no_limit_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    # Five dollars for one request: far above any old reserve or ceiling.
    first = await broker.execute(_request("expensive"), _charged("5"))
    assert first["choices"][0]["message"]["content"] == "answer"
    second = await broker.execute(_request("after-expensive"), _charged("0.000001"))
    assert second["choices"][0]["message"]["content"] == "answer"
    snapshot = await repository.inference_accounting_snapshot()
    rows = {row["operation_id"]: row for row in snapshot["operations"]}
    assert rows["expensive"]["state"] == "settled"
    assert rows["expensive"]["actual_cost_microusd"] == 5_000_000
    assert rows["expensive"]["recovery_reason"] is None
    assert snapshot["spent_microusd"] == 5_000_001


@pytest.mark.asyncio
async def test_unpriced_answer_is_delivered_lane_freed_and_job_not_parked(accounting_db):
    _no_limit_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    result = await broker.execute(_request("unpriced"), _unpriced)
    assert result["choices"][0]["message"]["content"] == "answer"
    snapshot = await repository.inference_accounting_snapshot()
    row = snapshot["operations"][0]
    assert row["state"] == "unknown"
    assert snapshot["unpriced_requests"] == 1
    assert snapshot["spent_microusd"] == 0
    job = await repository.get_job(row["job_id"])
    assert job["status"] == "succeeded"
    # The lane is free: no operation is active and the next request runs.
    assert (await broker.status())["active"] is None
    follow_up = await broker.execute(_request("after-unpriced"), _charged("0.000002"))
    assert follow_up["choices"][0]["message"]["content"] == "answer"
    # Recording the charge later is optional and only fills the history in.
    await repository.settle_inference_cost(row["operation_id"], job_id=row["job_id"],
        expected_revision=row["revision"], actual_cost_microusd=4, evidence_digest="b" * 64,
        operator_id="operator:settings-owner", idempotency_key="record-1")
    after = await repository.inference_accounting_snapshot()
    assert (after["unpriced_requests"], after["spent_microusd"]) == (0, 6)


def _legacy_reseal(root: Path, *, mutate_rows, history_extra):
    """Rewrite the ledger the way a pre-#1284 build sealed it (v1 totals)."""
    from src.workspace import accounting_witness as codec
    from src.workspace.production import ProductionWorkspace, read_lifecycle_receipt, write_lifecycle_receipt
    with sqlite3.connect(root / "seraph.db") as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT * FROM inference_cost_reservations ORDER BY operation_id")]
        account = dict(db.execute("SELECT * FROM inference_accounting_owners").fetchone())
        rows = mutate_rows(rows)
        for row in rows:
            row[codec.ROW_DIGEST_COLUMN] = codec.row_digest(row)
            db.execute("UPDATE inference_cost_reservations SET " + ", ".join(f'"{key}"=?' for key in row)
                + " WHERE operation_id=?", (*row.values(), row["operation_id"]))
        history = json.loads(account["settings_history_json"]) + history_extra
        account["settings_history_json"] = json.dumps(history, sort_keys=True, separators=(",", ":"))
        account[codec.TOTALS_COLUMN] = json.dumps(codec.ledger_totals(rows, 1), sort_keys=True, separators=(",", ":"))
        account["revision"] += 1
        root_digest = codec.rows_root(codec.row_digest(row) for row in rows)
        account["ledger_digest"] = codec.v2_ledger_digest(account, root_digest)
        db.execute("UPDATE inference_accounting_owners SET " + ", ".join(f'"{key}"=?' for key in account)
            + " WHERE deployment_id=?", (*account.values(), account["deployment_id"]))
        db.commit()
    workspace = ProductionWorkspace(host_root=root)
    receipt = read_lifecycle_receipt(workspace) or {}
    receipt["inference_accounting"] = codec.witness(account)
    with codec.maintenance_accounting_lock(root):
        write_lifecycle_receipt(workspace, receipt, _accounting_lock_held=True)
    return account


@pytest.mark.asyncio
async def test_old_ledger_with_reviews_overruns_and_v1_totals_reseals_and_keeps_history(accounting_db):
    from src.workflows import inference_ledger
    root, _engine, _factory = accounting_db
    _no_limit_configuration()
    repository = DurableJobRepository()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    await broker.execute(_request("old-overrun"), _charged("0.000500"))
    await broker.execute(_request("old-normal"), _charged("0.000040"))
    await broker.execute(_request("old-unknown"), _unpriced)

    def as_old_build(rows):
        for row in rows:
            row["ceiling_microusd"], row["bound_microusd"] = 1000, 100
            if row["operation_id"] == "old-overrun":
                row["period_id"] = "2026-09"
                row["recovery_reason"] = "provider_charge_exceeded_reservation"
            if row["operation_id"] == "old-normal":
                row["period_id"] = "2026-10"
            if row["operation_id"] == "old-unknown":
                row["owner_ceiling_microusd"] = 1000
        return rows

    _legacy_reseal(root, mutate_rows=as_old_build, history_extra=[
        {"kind": "period_initialized", "period_id": "2026-09", "recorded_at": "2026-09-01T00:00:00+00:00"},
        {"kind": "period_review", "period_id": "2026-10", "accounting_revision": 1, "actor": "operator:old",
         "recorded_at": "2026-10-01T00:00:00+00:00", "memory_status": "no_learning"},
        {"kind": "request_reserve_review", "revision": 1, "accounting_revision": 2, "bound_microusd": 50,
         "operations": [], "recorded_at": "2026-10-02T00:00:00+00:00"}])
    inference_ledger.reset_process_state()
    before = {"2026-09": 500, "2026-10": 40}

    # Startup recovery verifies the v1 totals and reseals them forward.
    await repository.recover_inference_accounting()
    with sqlite3.connect(root / "seraph.db") as db:
        sealed = json.loads(db.execute("SELECT ledger_totals_json FROM inference_accounting_owners").fetchone()[0])
        history = json.loads(db.execute("SELECT settings_history_json FROM inference_accounting_owners").fetchone()[0])
    assert sealed["schema_version"] == 2 and "overruns" not in sealed
    assert {key: sealed["settled_by_period"][key] for key in before} == before
    assert sealed["open_rows_by_state"]["unknown"] == 1
    # The old review entries stay in the hashed account history, untouched.
    assert [entry["kind"] for entry in history if entry.get("kind") in {"period_review", "request_reserve_review"}] == [
        "period_review", "request_reserve_review"]
    for period, spent in before.items():
        month = datetime.strptime(period + "-15", "%Y-%m-%d").replace(tzinfo=timezone.utc)
        assert (await repository.inference_accounting_snapshot(now=month))["spent_microusd"] == spent

    # The next request is admitted: no month review, overrun or reserve stops it.
    after = await broker.execute(_request("after-upgrade"), _charged("0.000003"))
    assert after["choices"][0]["message"]["content"] == "answer"
    current = await repository.inference_accounting_snapshot()
    assert current["status"] == "ready" and current["unpriced_requests"] == 1
    assert current["spent_microusd"] == 3


@pytest.mark.asyncio
async def test_month_change_and_clock_set_back_neither_block_nor_lose_totals(accounting_db):
    root, _engine, _factory = accounting_db
    _no_limit_configuration()
    repository = DurableJobRepository()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    await broker.execute(_request("earlier-month"), _charged("0.000010"))
    await broker.execute(_request("later-month"), _charged("0.000020"))

    def two_months(rows):
        for row in rows:
            # One charge from an earlier month and one from a later month than
            # today's clock (as after the clock was set back).
            row["period_id"] = "2001-01" if row["operation_id"] == "earlier-month" else "2999-12"
        return rows

    _legacy_reseal(root, mutate_rows=two_months, history_extra=[
        {"kind": "period_observed", "period_id": "2999-12", "recorded_at": "2999-12-01T00:00:00+00:00"}])
    from src.workflows import inference_ledger
    inference_ledger.reset_process_state()
    # Admitted with no new-month or clock-correction review.
    result = await broker.execute(_request("today"), _charged("0.000004"))
    assert result["choices"][0]["message"]["content"] == "answer"
    today = await repository.inference_accounting_snapshot()
    assert today["status"] == "ready" and today["spent_microusd"] == 4
    assert (await repository.inference_accounting_snapshot(
        now=datetime(2001, 1, 9, tzinfo=timezone.utc)))["spent_microusd"] == 10
    assert (await repository.inference_accounting_snapshot(
        now=datetime(2999, 12, 9, tzinfo=timezone.utc)))["spent_microusd"] == 20


def _legacy_settings_payload():
    """A model-fabric-settings.json written before #1284, with every amount."""
    from src.model_fabric.configuration import _configuration_payload, InferenceLaneConfig, NearTeeSetup, NearTeeSlot, NearTextSetup
    configuration = ModelFabricConfiguration(status="ready", egress_revision=4,
        inference_lane=InferenceLaneConfig(spend_ceiling_microusd=2_000_000),
        near_text=NearTextSetup(spend_ceiling_microusd=2_000_000, request_cost_bound_microusd=30_000),
        near_tee=NearTeeSetup(credential_fingerprint="0123456789ab", slots={"text": NearTeeSlot(
            model_id="Qwen/Qwen3.8-27B", max_input_tokens=8192, max_output_tokens=1024,
            request_cost_bound_microusd=20_000, enabled=True, purpose_consent_revision=4)}))
    payload = _configuration_payload(configuration)
    # Exactly the pre-#1284 shape: every amount present, the reserve review null.
    assert payload["inference_lane"]["spend_ceiling_microusd"] == 2_000_000
    assert "request_reserve_review_microusd" in payload["inference_lane"]
    assert payload["near_tee"]["slots"]["text"]["request_cost_bound_microusd"] == 20_000
    return payload


def test_old_settings_file_with_every_cost_field_loads_unchanged_and_keeps_consent(tmp_path):
    from src.model_fabric.configuration import (
        _configuration_payload, _configuration_from_payload, near_tee_profile_for_slot, near_tee_slot_block_reason,
    )
    from src.workspace.accounting_witness import configuration_digest
    payload = _legacy_settings_payload()
    path = tmp_path / "model-fabric-settings.json"
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    path.write_text(raw)
    configured = _configuration_from_payload(json.loads(raw))
    # Read past, never rewritten: the bytes on disk and the witnessed policy
    # digest stay exactly what the lifecycle receipt recorded.
    assert path.read_text() == raw
    assert configuration_digest(_configuration_payload(configured)) == configuration_digest(json.loads(raw))
    # Consent is still current and the slot is not blocked for any amount.
    assert near_tee_slot_block_reason(configured, "text") is None
    # The stored slot keeps its exact profile contract, so proofs stay bound.
    slot = configured.near_tee.slots["text"]
    assert near_tee_profile_for_slot("text", slot).options["_seraph_near_tee"]["request_cost_bound_microusd"] == 20_000


def test_new_settings_carry_no_amounts_and_need_no_lane():
    from src.model_fabric.configuration import (
        _configuration_payload, _configuration_from_payload, NearTeeSetup, NearTeeSlot, near_tee_profile_for_slot,
        near_tee_slot_block_reason,
    )
    configuration = ModelFabricConfiguration(status="ready", egress_revision=2,
        near_tee=NearTeeSetup(credential_fingerprint="0123456789ab", slots={"text": NearTeeSlot(
            model_id="Qwen/Qwen3.8-27B", max_input_tokens=8192, max_output_tokens=1024,
            enabled=True, purpose_consent_revision=2)}))
    payload = _configuration_payload(configuration)
    assert "inference_lane" not in payload
    assert "request_cost_bound_microusd" not in payload["near_tee"]["slots"]["text"]
    reloaded = _configuration_from_payload(json.loads(json.dumps(payload)))
    assert near_tee_slot_block_reason(reloaded, "text") is None
    controls = near_tee_profile_for_slot("text", reloaded.near_tee.slots["text"]).options["_seraph_near_tee"]
    assert "request_cost_bound_microusd" not in controls


@pytest.mark.asyncio
async def test_tampered_ledger_row_records_a_history_gap_and_the_request_still_runs(accounting_db):
    """Design test 11 (#1284 part 2): ledger continuity is display only."""
    root, _engine, _factory = accounting_db
    _no_limit_configuration()
    repository = DurableJobRepository()
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    await broker.execute(_request("before-tamper"), _charged("0.000010"))
    # Edit a sealed row's content behind the ledger's back (as a restored or
    # hand-edited database would): its stored digest no longer matches.
    with sqlite3.connect(root / "seraph.db") as db:
        db.execute("UPDATE inference_cost_reservations SET actual_cost_microusd=999 WHERE operation_id='before-tamper'")
        db.commit()
    from src.workflows import inference_ledger
    inference_ledger.reset_process_state()
    result = await broker.execute(_request("after-tamper"), _charged("0.000002"))
    assert result["choices"][0]["message"]["content"] == "answer"
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["status"] == "ready"
    assert snapshot["history_verified"] is False and snapshot["history_gap_since"]
    # The reseal keeps what is present: the edited amount is now the history.
    assert snapshot["spent_microusd"] == 999 + 2
    with sqlite3.connect(root / "seraph.db") as db:
        history = json.loads(db.execute("SELECT settings_history_json FROM inference_accounting_owners").fetchone()[0])
    assert [entry["kind"] for entry in history].count("history_gap") == 1
    # The resealed ledger verifies cleanly again and stays usable.
    inference_ledger.reset_process_state()
    again = await broker.execute(_request("after-reseal"), _charged("0.000001"))
    assert again["choices"][0]["message"]["content"] == "answer"
    with sqlite3.connect(root / "seraph.db") as db:
        history = json.loads(db.execute("SELECT settings_history_json FROM inference_accounting_owners").fetchone()[0])
    assert [entry["kind"] for entry in history].count("history_gap") == 1


@pytest.mark.asyncio
async def test_moved_workspace_without_rebind_still_blocks_as_a_consent_fence(accounting_db, monkeypatch):
    """Design test 9: the deployment binding is a consent fence, unchanged."""
    from src.workflows.inference_accounting import InferenceAccountingError
    from src.workspace import accounting_witness
    _no_limit_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()

    def moved(_workspace):
        raise accounting_witness.ProductionWorkspaceReconciliationError("accounting_deployment_binding_unavailable")
    monkeypatch.setattr(accounting_witness, "assert_deployment_binding", moved)
    calls = []

    async def transport():
        calls.append("contact")
        return {"id": "gen-x", "usage": {"cost": "0"}}
    with pytest.raises(Exception) as denied:
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(_request("moved"), transport)
    assert calls == []
    # The binding guards the provider policy record: a consent fence, not money.
    assert str(denied.value) == "provider_policy_continuity_unavailable"
    del InferenceAccountingError


def test_cockpit_bound_zero_stop_fixture_is_the_backend_evidence_shape():
    """A new automatic repository stop records a 0 legacy server bound. The
    cockpit fixture for it must be exactly what the backend contract and
    digest serialize, so the inspector is tested against the real shape."""
    from src.work_board.contracts import RepositoryNativeLimitEvidenceV1
    from src.workflows.repo_repair_source import _source_digest
    fixture = json.loads((REPO_ROOT / "frontend" / "src" / "test" / "fixtures"
        / "repository-stop-bound-zero.json").read_text())["repository_stop"]
    evidence = RepositoryNativeLimitEvidenceV1(original_limits_digest=_source_digest({"fixture": "bound-zero"}),
        original_deadline_at="2026-11-19T10:00:00+00:00", original_server_bound_microusd=0,
        root_liability_microusd=0, group_liability_microusd=0, group_calls=1,
        original_root_max_cost_microusd=0, original_group_max_cost_microusd=0, original_group_max_calls=5,
        goal_cutoff_at=None, cause="deadline_exhausted").model_dump(mode="json")
    assert fixture == {"reason": "deadline_exhausted", "pending": False,
        "limit_evidence": evidence, "limit_evidence_digest": _source_digest(evidence)}
    assert evidence["original_server_bound_microusd"] == 0


@pytest.mark.parametrize("evidence, shown", [
    # The ledger evidence restart recovery wrote before #1284 (0ab60c3b..#1284)
    # always carried the row's new reason (#1302 review).
    ([{"kind": "reservation"}, {"kind": "restart_recovery", "state": "unknown",
        "reason": "provider_cost_readback_required"}], "provider_outcome_unknown"),
    ([{"kind": "reservation"}], "provider_cost_readback_required"),
    # Append-only evidence: an earlier typed precontact resume, then an
    # answered unpriced call, is still "answered, not priced".
    ([{"kind": "reservation"}, {"kind": "restart_recovery", "state": "reserved",
        "reason": "typed_owner_precontact_resume"}], "provider_cost_readback_required"),
])
def test_rows_recovered_before_the_reason_split_show_an_unknown_outcome(evidence, shown):
    """A row restart recovery classified before #1284 split the reasons keeps
    ``provider_cost_readback_required`` on disk; Spend history must not call it
    "answered, not priced". Only the operator projection changes."""
    from src.db.models import InferenceCostReservation
    from src.workflows.inference_accounting import _operation_payload, _operator_operation
    row = InferenceCostReservation(operation_id="op", job_id="job", owner_id="service:x", state="unknown",
        recovery_reason="provider_cost_readback_required", revision=1, evidence_json=json.dumps(evidence))
    assert _operator_operation(row)["recovery_reason"] == shown
    assert _operation_payload(row)["recovery_reason"] == "provider_cost_readback_required"
