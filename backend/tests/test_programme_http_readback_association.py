"""Original retained HTTP/artifact association mechanics; no Source issuance."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
from uuid import UUID

import pytest

from src.guardian.research_plan_contracts import SearchManifestV1, SourceSelectionV1, PublicSnapshotV1
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, GOAL
from src.work_board.research_artifacts import stage_discovery_artifact, json_bytes, sha
from src.work_board.research_parent import DISCOVERY_KIND
from src.workflows.job_runtime import _digest
from src.workspace.accounting_witness import _programme_snapshot_http_readback_digest
from src.workspace.production import ProductionWorkspaceReconciliationError


@pytest.fixture
def original_snapshot(tmp_path, monkeypatch):
    """Actual typed/staged bytes and native producer receipt schema, data only.

    GoalDiscoveryService._readback supplies target/fence/verification; the
    durable repository stores one replaced effect at the original intent ID.
    Neither this fixture nor the projection creates programme authority.
    """
    from config.settings import settings
    os.chmod(tmp_path, 0o700)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    programme_id, job_id = "1" * 32, "goal-discovery:" + "a" * 64
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    run_id = UUID("2" * 32)
    urls = ("https://example.com/original-first", "https://example.com/original-second")
    result_ids = tuple(sha(url.encode())[:32] for url in urls)

    def build(*, reverse=False, slot=0, different_selected_result=False, foreign_url=False):
        staged, records = [], []
        def adopt(kind, value, *, index=0):
            artifact = stage_discovery_artifact(programme_id=programme_id, job_id=job_id,
                kind=kind, slot=index, content=json_bytes(value))
            payload = {"artifact_ref": artifact.reference.model_dump(mode="json"),
                "file_path": artifact.file_path, "kind": kind, "slot": index,
                "job_id": job_id, "programme_id": programme_id,
                "byte_count": len(artifact.content), "producer_fence": 7, "no_learning": True}
            record = {"checkpoint_id": f"discovery:artifact:{kind}:{index}", "payload": payload,
                "artifact": {"artifact_id": artifact.reference.artifact_id,
                    "artifact_type": "goal_discovery_" + kind, "file_path": artifact.file_path,
                    "content_sha256": artifact.reference.digest, "size_bytes": len(artifact.content),
                    "producer": DISCOVERY_KIND, "exists": True},
                "effect": {"effect_id": "discovery-artifact:" + artifact.reference.artifact_id,
                    "receipt_kind": "readback", "effect_type": "research_artifact_readback",
                    "readback_id": "discovery-readback-" + artifact.reference.artifact_id,
                    "target_path": artifact.file_path, "target_digest": artifact.reference.digest,
                    "content_sha256": artifact.reference.digest, "status": "succeeded",
                    "verified_at": now.isoformat(), "recorded_at": now.isoformat(),
                    "fencing_token": 7, "reconciled": True, "reconciliation_status": "resolved",
                    "details": {"verified": True, "no_learning": True}}}
            staged.append(artifact)
            records.append(record)
            return artifact, record
        manifest = SearchManifestV1(run_id=run_id, query_digest="3" * 64,
            results=[{"result_id": identifier, "exact_url": url, "title": "Original public result",
                "observed_at": now} for identifier, url in zip(result_ids, urls)])
        manifest_artifact, _manifest_record = adopt("manifest", manifest.model_dump(mode="json"))
        selected = list(reversed(result_ids)) if reverse else list(result_ids)
        selection = SourceSelectionV1(run_id=run_id, manifest_ref=manifest_artifact.reference,
            selected_result_ids=selected)
        adopt("selection", selection.model_dump(mode="json"))
        result_id = selected[1 - slot] if different_selected_result else selected[slot]
        selected_url = dict(zip(result_ids, urls))[result_id]
        lines = ["Original normalized public source text"]
        snapshot = PublicSnapshotV1(result_id=result_id,
            url="https://example.org/foreign" if foreign_url else selected_url,
            digest=sha("\n".join(lines).encode()), lines=lines, fetched_at=now, mime="text/html")
        artifact, record = adopt("snapshot", snapshot.model_dump(mode="json"), index=slot)
        target_url = dict(zip(result_ids, urls))[selected[slot]]
        effect_id = f"discovery-source:{job_id}:{slot}"
        # Raw HTML response hash deliberately differs from normalized text and
        # canonical snapshot JSON hash, as the original producer actually does.
        raw_http_sha = sha(b"<html><body>Original normalized public source text</body></html>")
        effect = {"effect_id": effect_id, "receipt_kind": "readback", "effect_type": "public_https_read",
            "target_path": target_url, "target_digest": sha(target_url.encode()), "approval_id": None,
            "adapter_idempotency_key": None, "status": "succeeded", "content_sha256": raw_http_sha,
            "details": {"verified": True, "read_only": True, "no_learning": True,
                "original_observation": "retained raw response"},
            "recorded_at": now.isoformat(), "fencing_token": 7,
            "readback_id": "discovery-http:" + effect_id, "verified_at": now.isoformat(),
            "reconciled": True, "reconciliation_status": "resolved"}
        return dict(root=tmp_path, job_id=job_id, programme_id=programme_id, record=record,
            records=records, effects=[effect]), tuple(staged), snapshot
    return build


def project(inputs, budget=None):
    return _programme_snapshot_http_readback_digest(**inputs,
        header_budget=budget if budget is not None else HeaderReadBudget())


@pytest.mark.parametrize("reverse,slot", [(False, 0), (False, 1), (True, 0), (True, 1)])
def test_whole_original_readback_joins_actual_persisted_selection(original_snapshot, monkeypatch, reverse, slot):
    inputs, staged, snapshot = original_snapshot(reverse=reverse, slot=slot)
    original = deepcopy(inputs["effects"][0])
    budget = HeaderReadBudget()
    # Exercise the original SQL-row ledger namespace, not file-path identities.
    # Enrollment here tests ledger mechanics and grants no row/body authority.
    budget.enroll({(object(), GOAL.table, 1)})
    sql_references = budget.physical_references.copy()
    debits = []
    original_debit = budget.debit
    def observe_debit(amount, *, appearance=None):
        debits.append((appearance, amount))
        return original_debit(amount, appearance=appearance)
    monkeypatch.setattr(budget, "debit", observe_debit)
    before = budget.remaining
    assert project(inputs, budget) == _digest(original)
    assert inputs["effects"] == [original]
    assert budget.remaining == before - sum(len(item.content) + 1 for item in staged)
    assert budget.physical_references == sql_references
    # Actual helper read order is snapshot, manifest, selection; each original
    # file appearance pays its full exact byte allowance independently.
    assert debits == [(None, len(item.content) + 1) for item in (staged[2], staged[0], staged[1])]
    assert len({original["content_sha256"], snapshot.digest, staged[-1].reference.digest}) == 3
    inputs["effects"][0]["details"]["original_observation"] = "another retained original observation"
    assert project(inputs) != _digest(original)


def test_insufficient_same_budget_denies_snapshot_before_any_file_body(original_snapshot, monkeypatch):
    from src.work_board import input_artifacts
    inputs, staged, _snapshot = original_snapshot()
    budget = HeaderReadBudget()
    budget.enroll({(object(), GOAL.table, 1)})
    sql_references = budget.physical_references.copy()
    snapshot_cost = len(staged[2].content) + 1
    # Spend the existing original frame, never reset or substitute its budget.
    budget.debit(budget.remaining - (snapshot_cost - 1))
    before = budget.remaining
    reads = []
    def unexpected_body_read(*args):
        reads.append(args)
        raise AssertionError("insufficient budget reached actual file body")
    monkeypatch.setattr(input_artifacts.os, "read", unexpected_body_read)
    with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
        project(inputs, budget)
    assert reads == []
    assert budget.remaining == before
    assert budget.physical_references == sql_references


@pytest.mark.parametrize("field,value", [
    ("readback_id", "discovery-http:foreign"),
    ("effect_type", "foreign-read"),
    ("target_path", "https://example.org/foreign"),
    ("target_digest", "f" * 64),
    ("fencing_token", 8),
    ("fencing_token", True),
    ("content_sha256", "not-an-original-sha256"),
    ("reconciled", False),
    ("reconciliation_status", "unresolved"),
    ("verified_at", "2026-10-10T00:00:00"),
])
def test_positive_looking_misbound_readback_never_becomes_http_evidence(original_snapshot, field, value):
    inputs, staged, _snapshot = original_snapshot()
    inputs["effects"][0][field] = value
    before = {item.file_path: hashlib.sha256((inputs["root"] / item.file_path).read_bytes()).hexdigest()
        for item in staged}
    with pytest.raises(ProductionWorkspaceReconciliationError, match="programme_snapshot_http_association_changed"):
        project(inputs)
    assert before == {item.file_path: hashlib.sha256((inputs["root"] / item.file_path).read_bytes()).hexdigest()
        for item in staged}


@pytest.mark.parametrize("flag", ["verified", "read_only", "no_learning"])
def test_original_positive_details_are_required(original_snapshot, flag):
    inputs, _staged, _snapshot = original_snapshot()
    inputs["effects"][0]["details"][flag] = False
    with pytest.raises(ProductionWorkspaceReconciliationError):
        project(inputs)


@pytest.mark.parametrize("mutation", ["different_selected_result", "foreign_url"])
def test_snapshot_payload_requires_exact_slot_result_and_url(original_snapshot, mutation):
    inputs, _staged, _snapshot = original_snapshot(**{mutation: True})
    with pytest.raises(ProductionWorkspaceReconciliationError, match="programme_snapshot_http_association_changed"):
        project(inputs)


def test_snapshot_original_payload_file_hash_cannot_be_rebound(original_snapshot):
    inputs, _staged, _snapshot = original_snapshot()
    inputs["record"]["artifact"]["content_sha256"] = "f" * 64
    with pytest.raises(ProductionWorkspaceReconciliationError, match="programme_snapshot_http_association_changed"):
        project(inputs)


def test_snapshot_and_http_cannot_alias_a_different_original_producer_fence(original_snapshot):
    inputs, _staged, _snapshot = original_snapshot()
    inputs["record"]["payload"]["producer_fence"] = 8
    inputs["effects"][0]["fencing_token"] = 8
    # The actual adopted artifact's original producer receipt still has7.
    with pytest.raises(ProductionWorkspaceReconciliationError, match="programme_snapshot_http_association_changed"):
        project(inputs)


def test_duplicate_positive_address_is_not_a_genuine_replaced_effect(original_snapshot):
    inputs, _staged, _snapshot = original_snapshot()
    inputs["effects"].append(deepcopy(inputs["effects"][0]))
    with pytest.raises(ProductionWorkspaceReconciliationError):
        project(inputs)


@pytest.mark.parametrize("state", ["absent", "intent", "unknown", "failed-observation", "foreign-address"])
def test_missing_original_positive_readback_keeps_artifact_and_null(original_snapshot, state):
    inputs, staged, _snapshot = original_snapshot()
    effect = inputs["effects"][0]
    if state == "absent":
        inputs["effects"] = []
    elif state == "foreign-address":
        effect["effect_id"] = f"discovery-source:{inputs['job_id']}:1"
        effect["readback_id"] = "discovery-http:" + effect["effect_id"]
    else:
        # Actual original intent/Unknown effect shape, not receipt_kind=intent.
        effect.update(receipt_kind="effect", status="intent" if state == "failed-observation" else state, content_sha256=None,
            details={"read_only": True, "no_learning": True})
        for field in ("readback_id", "verified_at", "reconciled", "reconciliation_status"):
            effect.pop(field, None)
        if state == "failed-observation":
            observation = deepcopy(effect)
            observation.update(receipt_kind="readback", status="failed", original_effect_id=effect["effect_id"],
                effect_id=effect["effect_id"] + ":readback:" + _digest({
                    "status": "failed", "target_path": effect["target_path"]})[:16],
                details={"readback_observation_only": True, "verified": False, "no_learning": True})
            inputs["effects"].append(observation)
    original = deepcopy(inputs["effects"])
    assert project(inputs) is None
    assert inputs["effects"] == original
    assert (inputs["root"] / staged[-1].file_path).read_bytes() == staged[-1].content
