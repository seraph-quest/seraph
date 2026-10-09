"""Cold provenance/file mechanics; these fixtures grant no live Source authority."""
import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_goal_discovery_contracts import valid_plan
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
from src.work_board.research_artifacts import read_discovery, stage_discovery_artifact, json_bytes, sha
from src.workflows.job_runtime import _digest
from src.workflows.research_sources import physical_discovery_closure, discovery_physical_records


@pytest.fixture
def cold_discovery(tmp_path, monkeypatch):
    from config.settings import settings
    from src.work_board.research_parent import GoalDiscoveryAuthority, DISCOVERY_KIND, DISCOVERY_SERVICE
    os.chmod(tmp_path, 0o700)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    value = valid_plan()
    issued = datetime(2020, 1, 1, tzinfo=timezone.utc)
    value.update(issued_at=issued.isoformat(), deadline_at=(issued + timedelta(seconds=300)).isoformat())
    programme = value["programme_id"].replace("-", "")
    job_id = "goal-discovery:" + "a" * 64
    brief = stage_discovery_artifact(programme_id=programme, job_id=job_id, kind="public_brief", slot=0, content=b"Original public facts")
    value["public_brief_digest"] = brief.reference.digest
    value["steps"][0]["input_refs"] = [brief.reference.model_dump(mode="json")]
    plan = stage_discovery_artifact(programme_id=programme, job_id=job_id, kind="plan", slot=0, content=json_bytes(value))
    binding = dict(goal_id=value["goal_id"], goal_revision=1, programme_id=programme, grant_revision=1,
        owner_identity_id="original-identity", issuer_root_id="original-root", issuer_principal_id="original-principal",
        brief_digest=brief.reference.digest, capability_id="guardian.goal-discovery.v1", route_epoch=1,
        route_digest="b" * 64, expires_at=(issued + timedelta(seconds=300)).isoformat(), cost_ceiling_microusd=1000)
    authority = GoalDiscoveryAuthority(authority_type="goal_programme_discovery_v1", principal=DISCOVERY_SERVICE,
        owner_kind="service", service_id=DISCOVERY_SERVICE, capability_id="guardian.goal-discovery.v1", capability_version="1",
        goal_owner_principal_id="original-principal", goal_owner_session_id="original-root", programme_binding=binding,
        plan_ref=plan.reference, occurrence_day="2020-01-01", original_job_id=job_id, budget_microusd=1000, no_learning=True).model_dump(mode="json")
    inputs = dict(plan_ref=plan.reference.model_dump(mode="json"), plan_file_path=plan.file_path,
        public_brief_ref=brief.reference.model_dump(mode="json"), public_brief_file_path=brief.file_path, no_learning=True)
    history, artifacts, effects = [], [], []
    for item in (brief, plan):
        history.append({"checkpoint_id": f"discovery:artifact:{item.kind}:0", "payload": {
            "artifact_ref": item.reference.model_dump(mode="json"), "file_path": item.file_path, "kind": item.kind,
            "slot": 0, "job_id": job_id, "programme_id": programme, "byte_count": len(item.content), "producer_fence": 0, "no_learning": True}})
        artifacts.append(dict(artifact_id=item.reference.artifact_id, artifact_type="goal_discovery_" + item.kind,
            file_path=item.file_path, content_sha256=item.reference.digest, size_bytes=len(item.content), producer=DISCOVERY_KIND, exists=True))
        effects.append(dict(effect_id="discovery-artifact:" + item.reference.artifact_id, receipt_kind="readback",
            effect_type="research_artifact_readback", status="succeeded", target_path=item.file_path,
            target_digest=item.reference.digest, content_sha256=item.reference.digest,
            readback_id="discovery-readback-" + item.reference.artifact_id, details={"verified": True, "no_learning": True}))
    row = dict(run_identity=job_id, job_kind=DISCOVERY_KIND, owner_kind="service", owner_principal_id=DISCOVERY_SERVICE,
        service_id=DISCOVERY_SERVICE, operator_session_id=None, session_id=None, goal_id=value["goal_id"], goal_revision=1,
        status="unknown_external_effect", deadline_at=value["deadline_at"], declared_authority_json=json.dumps(authority),
        authority_digest=_digest(authority), arguments_json=json.dumps(inputs), input_digest=_digest(inputs),
        checkpoint_receipts_json=json.dumps(history), artifact_receipts_json=json.dumps(artifacts), effect_receipts_json=json.dumps(effects))
    return tmp_path, row, (brief, plan)


@pytest.mark.parametrize("status", ["blocked", "failed", "succeeded", "unknown_external_effect"])
def test_cold_original_history_reads_each_appearance_in_same_budget(cold_discovery, status):
    root, row, artifacts = cold_discovery
    row["status"] = status
    budget = HeaderReadBudget()
    before = budget.remaining
    appearances = physical_discovery_closure(row, root=root, header_budget=budget)
    assert len(appearances) == 4
    assert before - budget.remaining == 2 * sum(len(item.content) + 1 for item in artifacts)
    assert len(discovery_physical_records(row)) == 2


@pytest.mark.parametrize("mutation", ["foreign_job", "missing_effect", "wrong_size", "wrong_slot", "foreign_goal"])
def test_cold_provenance_denies_before_file_read(cold_discovery, monkeypatch, mutation):
    root, original, _ = cold_discovery
    row = deepcopy(original)
    if mutation == "foreign_job": row["run_identity"] = "goal-discovery:foreign"
    elif mutation == "foreign_goal": row["goal_id"] = "foreign"
    elif mutation == "missing_effect": row["effect_receipts_json"] = "[]"
    elif mutation == "wrong_size":
        records = json.loads(row["artifact_receipts_json"]); records[0]["size_bytes"] += 1; row["artifact_receipts_json"] = json.dumps(records)
    else:
        records = json.loads(row["checkpoint_receipts_json"]); records[0]["payload"]["slot"] = 1; row["checkpoint_receipts_json"] = json.dumps(records)
    monkeypatch.setattr("src.work_board.research_artifacts.read_discovery", lambda *args, **kwargs: pytest.fail("physical read preceded provenance"))
    with pytest.raises(ValueError): physical_discovery_closure(row, root=root, header_budget=HeaderReadBudget())


def test_budget_exhaustion_denies_before_actual_file_open(cold_discovery, monkeypatch):
    root, _, artifacts = cold_discovery
    item = artifacts[0]
    budget = HeaderReadBudget(); budget.remaining = len(item.content)
    monkeypatch.setattr("src.work_board.input_artifacts._open_input_artifact_parent", lambda *args, **kwargs: pytest.fail("opened before debit"))
    with pytest.raises(HeaderBoundsError):
        read_discovery(item.file_path, item.reference.digest, programme_id=item.programme_id, root=root,
            expected_size=len(item.content), header_budget=budget)


@pytest.mark.parametrize("corruption", ["missing", "digest", "fifo", "symlink"])
def test_original_safe_reader_rejects_missing_corrupt_and_nonregular_files(cold_discovery, corruption):
    from src.work_board.repository import BoardError
    root, _, artifacts = cold_discovery
    item = artifacts[0]; path = root / item.file_path
    path.unlink()
    if corruption == "digest": path.write_bytes(b"x" * len(item.content)); path.chmod(0o600)
    elif corruption == "fifo": os.mkfifo(path, 0o600)
    elif corruption == "symlink": path.symlink_to(root / artifacts[1].file_path)
    with pytest.raises(BoardError):
        read_discovery(item.file_path, item.reference.digest, programme_id=item.programme_id, root=root,
            expected_size=len(item.content), header_budget=HeaderReadBudget())


@pytest.fixture
def selected_snapshot_history(cold_discovery):
    """Persist actual bounded artifacts; HTTP journals are cold mechanics fixtures."""
    from uuid import UUID
    from src.guardian.discovery_search import SEARCH_URL
    from src.guardian.research_plan_contracts import SearchManifestV1, SourceSelectionV1, PublicSnapshotV1
    from src.work_board.research_parent import DISCOVERY_KIND
    root, original, initial = cold_discovery
    plan = json.loads(initial[1].content)
    programme = plan["programme_id"].replace("-", "")
    job_id = original["run_identity"]
    observed = datetime.fromisoformat(plan["issued_at"])
    query = "reviewed public query"
    urls = ["https://example.com/first", "https://example.com/second", "https://example.com/unselected"]
    result_ids = [sha(url.encode())[:32] for url in urls]

    def build(*, mutation=None, reverse_selection=False, missing_source_readback=False):
        row = deepcopy(original)
        history = json.loads(row["checkpoint_receipts_json"])
        artifacts = json.loads(row["artifact_receipts_json"])
        effects = json.loads(row["effect_receipts_json"])
        staged = list(initial)

        def adopt(kind, value, *, slot=0, search_derivation=None):
            item = stage_discovery_artifact(programme_id=programme, job_id=job_id,
                kind=kind, slot=slot, content=json_bytes(value))
            payload = {"artifact_ref": item.reference.model_dump(mode="json"), "file_path": item.file_path,
                "kind": kind, "slot": slot, "job_id": job_id, "programme_id": programme,
                "byte_count": len(item.content), "producer_fence": 1, "no_learning": True}
            if search_derivation is not None:
                payload["search_derivation"] = {**search_derivation, "manifest_ref": item.reference.model_dump(mode="json")}
            history.append({"checkpoint_id": f"discovery:artifact:{kind}:{slot}", "payload": payload})
            artifacts.append(dict(artifact_id=item.reference.artifact_id, artifact_type="goal_discovery_" + kind,
                file_path=item.file_path, content_sha256=item.reference.digest, size_bytes=len(item.content),
                producer=DISCOVERY_KIND, exists=True))
            effects.append(dict(effect_id="discovery-artifact:" + item.reference.artifact_id,
                receipt_kind="readback", effect_type="research_artifact_readback", status="succeeded",
                target_path=item.file_path, target_digest=item.reference.digest, content_sha256=item.reference.digest,
                readback_id="discovery-readback-" + item.reference.artifact_id,
                details={"verified": True, "no_learning": True}))
            staged.append(item)
            return item

        def http_pair(effect_id, url, content_digest, *, response_receipt=None, missing=False):
            effects.append(dict(effect_id=effect_id, receipt_kind="intent", effect_type="public_https_read",
                status="intent", target_path=url, target_digest=sha(url.encode()), fencing_token=1,
                details={"no_learning": True}))
            if not missing:
                details = {"verified": True, "no_learning": True}
                if response_receipt is not None:
                    details["search_response_receipt"] = response_receipt
                effects.append(dict(effect_id=effect_id, receipt_kind="readback", effect_type="public_https_read",
                    status="succeeded", target_path=url, target_digest=sha(url.encode()), fencing_token=1,
                    content_sha256=content_digest, readback_id="discovery-http:" + effect_id, details=details))

        adopt("queries", {"queries": [query]})
        receipt = dict(query_index=0, query_digest=sha(query.encode()), response_digest=sha(b"original public search response"), byte_count=31)
        http_pair(f"discovery-search:{job_id}:0", SEARCH_URL, receipt["response_digest"], response_receipt=receipt)
        manifest = SearchManifestV1(run_id=UUID(plan["plan_id"]), query_digest=sha(query.encode()),
            results=[dict(result_id=identifier, exact_url=url, title=f"Public result {index}", observed_at=observed)
                for index, (identifier, url) in enumerate(zip(result_ids, urls))])
        manifest_artifact = adopt("manifest", manifest.model_dump(mode="json"),
            search_derivation={"query_digest": manifest.query_digest, "responses": [receipt]})
        selected = result_ids[:2][::-1] if reverse_selection else result_ids[:2]
        selection = SourceSelectionV1(run_id=manifest.run_id, manifest_ref=manifest_artifact.reference,
            selected_result_ids=selected)
        adopt("selection", selection.model_dump(mode="json"))
        known = dict(zip(result_ids, urls))
        for index, identifier in enumerate(selected):
            slot = index
            url = known[identifier]
            if index == 0:
                if mutation == "selected_index":
                    identifier, url = selected[1], known[selected[1]]
                elif mutation == "foreign_url":
                    url = "https://example.org/foreign"
                elif mutation == "unselected_result":
                    identifier, url = result_ids[2], urls[2]
                elif mutation == "out_of_selection_slot":
                    slot = 2
            if mutation == "swapped_slots":
                slot = 1 - index
            lines = [f"Original public source at selected index {index}"]
            snapshot = PublicSnapshotV1(result_id=identifier, url=url, digest=sha("\n".join(lines).encode()),
                lines=lines, fetched_at=observed, mime="text/plain")
            adopt("snapshot", snapshot.model_dump(mode="json"), slot=slot)
            http_pair(f"discovery-source:{job_id}:{index}", known[selected[index]], snapshot.digest,
                missing=missing_source_readback and index == 0)
        row.update(checkpoint_receipts_json=json.dumps(history), artifact_receipts_json=json.dumps(artifacts),
            effect_receipts_json=json.dumps(effects))
        return root, row, tuple(staged), selected
    return build


@pytest.mark.parametrize("reverse_selection", (False, True))
@pytest.mark.parametrize("missing_source_readback", (False, True))
def test_original_snapshot_slot_uses_persisted_selection_order_and_retains_crash_artifact(
        selected_snapshot_history, reverse_selection, missing_source_readback):
    root, row, staged, selected = selected_snapshot_history(reverse_selection=reverse_selection,
        missing_source_readback=missing_source_readback)
    budget = HeaderReadBudget()
    before = budget.remaining
    appearances = physical_discovery_closure(row, root=root, header_budget=budget)
    assert len(appearances) == len(staged) + 2
    assert before - budget.remaining == sum(len(item.content) + 1 for item in (*staged, *staged[:2]))
    snapshots = sorted((item for item in staged if item.kind == "snapshot"), key=lambda item: item.slot)
    assert [json.loads(item.content)["result_id"] for item in snapshots] == selected
    if missing_source_readback:
        effects = json.loads(row["effect_receipts_json"])
        source_id = "discovery-source:" + row["run_identity"] + ":0"
        assert any(item["effect_id"] == source_id and item["receipt_kind"] == "intent" for item in effects)
        assert not any(item["effect_id"] == source_id and item["receipt_kind"] == "readback" for item in effects)


@pytest.mark.parametrize("mutation", ("selected_index", "swapped_slots", "out_of_selection_slot", "foreign_url", "unselected_result"))
def test_original_persisted_snapshot_denies_misbound_selected_index_slot_or_url(selected_snapshot_history, mutation):
    root, row, _staged, _selected = selected_snapshot_history(mutation=mutation)
    with pytest.raises(ValueError, match="snapshot slot, result and URL"):
        physical_discovery_closure(row, root=root, header_budget=HeaderReadBudget())
